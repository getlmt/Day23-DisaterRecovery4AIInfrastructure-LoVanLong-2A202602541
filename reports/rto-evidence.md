# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

Môi trường: WSL Ubuntu 22.04, bare mode (`scripts/up_bare.sh`), chaos `--mode netblock --mock`
(SIGSTOP), `EDGE_TTL_SECONDS=5`, `WARMUP_SECONDS=6`. Mọi giờ ISO là UTC, ngày 2026-10-10.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-10T09:50:06` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.0s (ReadTimeout, 2036ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có — 16/32 request fail, dòng 17→32 đều `ok:false` | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 (`2026-10-10T09:58:10`) | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.0s | dòng `ok:false` đầu (ReadTimeout 2040ms) | `reports/drill-2-withdr.jsonl:25` |
| Runbook bắt đầu failover | +12.4s | `step:1_verify_target` (runbook đã xác nhận 3 probe fail) | `reports/failover-events.jsonl:5` |
| Snapshot restore xong | +12.5s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:6` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Region phụ ready | +18.6s | `step:4_wait_ready` (`waited_s` 6.12) | `reports/failover-events.jsonl:8` |
| DNS cutover | +18.6s | `step:5_dns_cutover` | `reports/failover-events.jsonl:9` |
| **RTO đo được** | **+22.5s** | dòng `ok:true` đầu sau lỗi, `served_by: b` | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 22.5s | 300s (5 phút) | PASS (`valid: true`, không warning) |
| RPO — Vector DB | 28.04s / 14 doc | 300s (5 phút) | PASS |

RPO lấy từ dòng `2_restore_snapshot` (`reports/failover-events.jsonl:6`): snapshot được dùng là
bản `put` lúc 09:57:53 (`reports/replication.jsonl:1`), 17.2s trước outage. Trong 14 doc mất:
8 doc ingest trước t_outage, 6 doc ingest sau t_outage (ingest vẫn ghi vào SQLite của region A
trong lúc API của A bị treo). Chu kỳ replicate tiếp theo chạy lúc 09:58:23
(`reports/replication.jsonl:2`) — chỉ 0.4s sau khi restore, nên RPO dao động rất mạnh
theo thời điểm restore so với chu kỳ 30s.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 12.3s trên đường găng · cấu hình 5s × 3 = 15s | `interval_s × threshold` = 5.0 × 3 trong `reports/health-events.jsonl:2`. Health checker phát hiện ở +15.0s. Runbook (chạy ngay sau lệnh kill) tự áp cùng luật 3 probe liên tiếp, cách 5s, timeout 2s → (3−1)×5 + 2 ≈ 12s, xác nhận ở +12.3s nên đây mới là phần nằm trên đường găng | Hạ interval (vd 2s × 3), probe từ nhiều điểm (quorum) để giữ chống flap; runbook dùng thẳng alert của health checker thay vì probe lại |
| Snapshot restore | 0.2s | +12.3s → +12.5s: thông báo incident, `1_verify_target`, copy SQLite + 2MB weights từ `state/_replica/` (`reports/failover-events.jsonl:6`) | Dữ liệu lab nhỏ nên đã nhanh; dữ liệu thật: replica chạy liên tục ở region phụ thay vì restore lúc failover |
| GPU pool warm-up | 6.1s | `waited_s` = 6.12 ở `4_wait_ready` (`reports/failover-events.jsonl:8`) — `WARMUP_SECONDS=6` tính từ lúc `3_scale_pool` đổi `warm → full` | Giữ pool region phụ ở `full` (hot standby) — đổi lại tốn GPU chạy không |
| DNS/LB TTL cache | 3.9s | t_recovered (+22.5s) − t_cutover (+18.6s): edge còn cache `a` tới khi TTL 5s hết, request trong lúc đó treo tới timeout 2s (`reports/drill-2-withdr.jsonl:35`) | Hạ `EDGE_TTL_SECONDS`; LB biết health (tự bỏ upstream timeout) |
| **Tổng** | **22.5s** | 12.3 + 0.2 + 6.1 + 3.9 = 22.5s = `rto_measured_s` | |
