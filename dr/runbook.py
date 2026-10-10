"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
# Xác nhận outage giống health checker: 3 probe liên tiếp cách nhau 5s, timeout 2s.
CONFIRM_PROBES, CONFIRM_INTERVAL_S, PROBE_TIMEOUT_S = 3, 5.0, 2.0
FAILOVER_WAIT_S = 60.0
GOLDEN_REQUESTS = 10


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N (mặc định N)."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # không có người ở terminal -> không tự failover
        return False


def alive(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=PROBE_TIMEOUT_S).status_code == 200
    except Exception:
        return False


def last_outage(region: str):
    """Sự kiện kill gần nhất của region trong chaos log (None nếu outage không do chaos)."""
    if not CHAOS.exists():
        return None
    kills = [json.loads(l) for l in CHAOS.read_text().splitlines() if l.strip()]
    kills = [e for e in kills if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1] if kills else None


def golden_signals(region: str, n: int) -> dict:
    lat, errors, served = [], 0, set()
    for i in range(n):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[region]}/v1/infer", params={"q": f"golden {i}"},
                          timeout=PROBE_TIMEOUT_S)
            body = r.json()
            if r.status_code != 200 or body.get("error"):
                errors += 1
            served.add(body.get("region"))
        except Exception:
            errors += 1
        lat.append((time.time() - t0) * 1000)
    lat.sort()
    p95 = lat[max(0, -(-len(lat) * 95 // 100) - 1)]  # nearest-rank
    return {"requests": n, "errors": errors, "error_rate": round(errors / n, 3),
            "p50_ms": round(lat[len(lat) // 2], 1), "p95_ms": round(p95, 1),
            "served_by": sorted(s for s in served if s)}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook. Failover chỉ được gọi 1 lần, ở bước 3."""
    t_start = time.time()
    out = {"ok": False, "primary": primary, "target": target}

    # 1 — đừng tin 1 lần fail: primary phải fail đủ CONFIRM_PROBES lần liên tiếp
    probes = []
    for i in range(CONFIRM_PROBES):
        t0 = time.time()
        ready, reason = hc.probe(primary, PROBE_TIMEOUT_S)
        probes.append({"n": i + 1, "ts": t0, "primary_ready": ready, "reason": reason})
        if ready:
            break
        if i < CONFIRM_PROBES - 1:
            time.sleep(max(0.0, CONFIRM_INTERVAL_S - (time.time() - t0)))
    t_ready, t_reason = hc.probe(target, PROBE_TIMEOUT_S)
    t_alive = alive(target)
    outage = len(probes) == CONFIRM_PROBES and not any(p["primary_ready"] for p in probes)
    step(1, "xac_nhan_outage", confirmed=outage, primary_probes=probes,
         target_alive=t_alive, target_ready=t_ready, target_reason=t_reason)
    if not outage or not t_alive:
        out["aborted_at"] = "1_xac_nhan_outage"
        out["why"] = ("primary van ready -> khong phai outage" if not outage
                      else f"region-{target} cung khong song -> double outage, khong failover")
        step(7, "post_incident", ok=False, **{k: out[k] for k in ("aborted_at", "why")},
             elapsed_s=round(time.time() - t_start, 2))
        return out

    # 2 — mở incident; ts dòng này = mốc operator biết tin, luôn sau t_outage
    k = last_outage(primary)
    t_outage = k["ts"] if k else probes[0]["ts"]
    s2 = step(2, "thong_bao_incident", severity="SEV1",
              message=f"region-{primary} down, chuan bi failover sang region-{target}",
              t_outage=t_outage, t_outage_iso=k["iso"] if k else None,
              t_outage_source="chaos/chaos-events.jsonl" if k else "probe_dau_tien_fail")
    notify_delay = round(s2["ts"] - t_outage, 2)
    out.update(t_outage=t_outage, t_announce=s2["ts"], notify_delay_s=notify_delay)

    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        out["aborted_at"] = "confirm"
        step(7, "post_incident", ok=False, aborted_at="confirm", why="operator khong confirm",
             elapsed_s=round(time.time() - t_start, 2))
        return out

    # 3 — gọi failover MỘT LẦN DUY NHẤT; nó tự ghi 5 bước con vào failover-events.jsonl
    res = fo.failover(target, backend, FAILOVER_WAIT_S)
    step(3, "scale_gpu_pool", ok=res.get("ok"), failover_steps=res.get("steps"),
         aborted_at=res.get("aborted_at"), notify_delay_s=notify_delay,
         confirmed_by="auto" if auto else "operator")

    # 4, 5 — chỉ ĐỌC lại kết quả của bước 3
    ts = res.get("target_state") or {}
    step(4, "verify_state_replica", ok=bool(ts.get("weights")) and (ts.get("count") or 0) > 0,
         vector_count=ts.get("count"), weights=ts.get("weights"), pool_state=ts.get("pool_state"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"))
    cut = res.get("cutover") or {}
    step(5, "dns_cutover", ok=bool(cut.get("ok")), active_region=cut.get("active_region"),
         active_region_from=cut.get("from"), waited_ready_s=res.get("waited_s"))

    # 6 — 10 request thật thẳng vào region phụ (edge có thể còn cache TTL trỏ về primary)
    gs = golden_signals(target, GOLDEN_REQUESTS) if res.get("ok") else None
    step(6, "verify_golden_signals", ok=bool(gs) and gs["errors"] == 0, region=target,
         **(gs or {"skipped": "failover khong ok"}))

    # 7
    out.update(ok=bool(res.get("ok")) and bool(gs) and gs["errors"] == 0,
               failover=res, golden_signals=gs, elapsed_s=round(time.time() - t_start, 2))
    step(7, "post_incident", ok=out["ok"], elapsed_s=out["elapsed_s"],
         since_outage_s=round(time.time() - t_outage, 2), notify_delay_s=notify_delay,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
