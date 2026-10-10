"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE = pathlib.Path("edge/active_region")
POLL_S = 0.5


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec), flush=True)
    return rec


def state_of(region: str) -> dict:
    """GET /v1/state; lỗi mạng -> dict có key `error` thay vì ném exception."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Bước nào hỏng -> dừng, KHÔNG đi tiếp tới cutover."""
    primary = "a" if target == "b" else "b"
    t_start = time.time()
    res = {"ok": False, "target": target, "primary": primary, "backend": backend, "steps": []}

    def done(step, **kw):
        res["steps"].append(step)
        return emit(step=step, target=target, **kw)

    # 1 — region phụ đang ở trạng thái nào? Không liên lạc được thì restore cũng vô ích.
    before = state_of(target)
    done("1_verify_target", ok="error" not in before, state=before)
    if "error" in before:
        res["aborted_at"] = "1_verify_target"
        return res

    # 2 — restore vector DB + weights từ snapshot, đo RPO thật (primary có mà bản restore không có)
    try:
        meta = snapshot.get(target, backend)
    except (SystemExit, Exception) as e:  # snapshot.get raise SystemExit khi chưa từng có `put`
        done("2_restore_snapshot", ok=False, error=str(e))
        res["aborted_at"] = "2_restore_snapshot"
        return res
    rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                       pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    done("2_restore_snapshot", ok=True, rpo_seconds=rpo["rpo_seconds"],
         docs_lost=rpo["docs_lost"], embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"), snapshot_latest_doc_ts=meta.get("latest_doc_ts"),
         primary_latest_doc_ts=rpo["primary_latest_doc_ts"])
    res.update(rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
               embed_model_version=meta.get("embed_model_version"))

    # 3 — bật GPU pool: warm -> full. serving/app.py bắt đầu đếm WARMUP_SECONDS từ lúc này.
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    prev_pool = pool.read_text().strip() if pool.exists() else "cold"
    pool.write_text("full")
    done("3_scale_pool", ok=True, pool_state_from=prev_pool, pool_state_to="full")

    # 4 — chờ /readyz 200. Timeout -> ABORT, tuyệt đối không cutover.
    t_wait, last = time.time(), "chua_probe"
    while True:
        try:
            r = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if r.status_code == 200:
                break
            last = ",".join(r.json().get("reasons") or []) or f"http_{r.status_code}"
        except Exception as e:
            last = type(e).__name__
        if time.time() - t_wait >= wait:
            done("4_wait_ready", ok=False, waited_s=round(time.time() - t_wait, 2),
                 wait_limit_s=wait, last_reason=last, note="ABORT: khong cutover")
            res["aborted_at"] = "4_wait_ready"
            return res
        time.sleep(POLL_S)
    waited = round(time.time() - t_wait, 2)
    done("4_wait_ready", ok=True, waited_s=waited, last_not_ready_reason=last)
    res["waited_s"] = waited

    # 5 — chỉ tới đây mới đổi "DNS". Không BOM, không newline.
    prev_active = ACTIVE.read_text(encoding="utf-8").strip() if ACTIVE.exists() else None
    ACTIVE.write_text(target, encoding="utf-8")
    done("5_dns_cutover", ok=True, active_region_from=prev_active, active_region_to=target)

    after = state_of(target)
    res.update(ok=True, cutover={"ok": True, "active_region": target, "from": prev_active},
               target_state={k: after.get(k) for k in ("pool_state", "weights", "count",
                                                       "latest_doc_ts", "error") if k in after},
               elapsed_s=round(time.time() - t_start, 2))
    return res


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
