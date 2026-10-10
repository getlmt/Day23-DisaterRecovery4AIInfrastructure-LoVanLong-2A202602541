# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

**Chạy ở đâu:** WSL Ubuntu, đứng ở thư mục gốc repo (mọi đường dẫn bên dưới là tương đối).
**Đường chính:** `python3 dr/runbook.py --primary a --target b --backend fs` — tự làm bước 1→7,
dừng hỏi `y/N` trước khi failover. Bảng dưới là các lệnh tay tương ứng, dùng để kiểm tra
từng bước hoặc làm tay khi script hỏng.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (chạy 3 lần, cách nhau 5s) · `tail -n 5 reports/health-events.jsonl` | `a.ready=false` cả 3 lần liên tiếp **và** `b.alive=true`; health log có dòng `"region": "a", "to": "UNHEALTHY"`. Nếu `b.alive=false` → DỪNG, đây là double outage, escalate | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` (dừng ở câu hỏi `y/N`) | Dòng `"name": "thong_bao_incident"` trong `reports/runbook-run.jsonl` có `t_outage` và `ts`; đã báo kênh incident (SEV1) | On-call SRE → báo Incident Commander (IC) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs` rồi `python3 state/snapshot.py get --region b --backend fs` | `curl -s localhost:8002/v1/state` có `count > 0`, `weights: true`; ghi lại `rpo_seconds`, `docs_lost`, `embed_model_version` (dòng `2_restore_snapshot` trong `reports/failover-events.jsonl`) | On-call SRE (IC đã trả lời `y`) |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` rồi `until curl -sf -o /dev/null localhost:8002/readyz; do sleep 0.5; done` | `/readyz` của b trả 200 (warm-up ≈ 6s). Quá 60s chưa 200 → **ABORT, KHÔNG làm bước 5**, escalate | On-call SRE |
| 5 | DNS/LB cutover | `printf b > edge/active_region` (CHỈ sau khi bước 4 = 200) | `curl -s localhost:8080/edge/state` cho `active_region=b` (chờ hết TTL 5s); `curl -s localhost:8080/v1/infer` trả `"edge_region":"b"` | On-call SRE, IC duyệt |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" localhost:8002/v1/infer; done` | 10/10 trả `200`, p95 < 200ms (drill đo 59.7ms), error rate = 0% (dòng `verify_golden_signals` trong `reports/runbook-run.jsonl`) | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid": true`, `"rto_verdict": "PASS"`, `warnings` rỗng; mở `reports/postmortem.md` trong 48h | IC |

**Rollback (failover ngược về region A):**

- *Trong lúc failover:* bước 4 quá 60s chưa ready, hoặc `2_restore_snapshot` lỗi (chưa có
  snapshot) → KHÔNG cutover, giữ `active_region=a`, escalate. Script `dr/failover.py` tự làm
  việc này (`aborted_at` trong output).
- *Sau cutover, quay lại A* chỉ khi đủ **cả 3**: (1) A trả `/readyz` 200 liên tục ≥ 15 phút
  (health log có `"region": "a", "to": "HEALTHY"` và không flap lại); (2) dữ liệu đã đối soát —
  doc ghi vào B trong lúc incident đã được đưa về A, và `docs_lost` của A đã được xử lý;
  (3) đang ở khung giờ ít traffic. Lệnh: `printf a > edge/active_region`, rồi lặp lại bước 5–6
  với region a.
- *Rollback khẩn* về A ngay cả khi chưa đủ 15 phút: B có error rate > 5% hoặc p95 > 500ms kéo
  dài 5 phút **và** A đã ready lại.
- **Ai quyết định:** Incident Commander (on-call lead). Không có failback tự động — tránh
  2 region flap qua lại (§4 Anti-Patterns). On-call SRE chỉ thực thi sau khi IC duyệt.
