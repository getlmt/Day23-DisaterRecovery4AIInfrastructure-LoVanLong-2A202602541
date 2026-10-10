# Postmortem — DR Drill Lab 23 (Drill 2, 2026-10-10)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** region-a bị treo (netblock/SIGSTOP) lúc 09:58:10 UTC. Runbook bán tự động
(`--auto` cho drill) restore snapshot vào region-b, warm-up pool, cutover DNS. User bị lỗi
22.5s (11 request fail), mất 14 doc (RPO 28.04s). Cả RTO và RPO đều trong mục tiêu 300s.

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-10T09:58:10 | outage bắt đầu — chaos kill region-a, `netblock` (SIGSTOP), region-b còn sống | `chaos/chaos-events.jsonl:3` |
| 2026-10-10T09:58:10 | user đầu tiên bị ảnh hưởng (+0.0s, ReadTimeout 2040ms) | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-10T09:58:22 | runbook xác nhận outage (3 probe liên tiếp timeout), mở incident SEV1, operator confirm cutover (`--auto`) → failover bắt đầu `1_verify_target` (+12.4s) | `reports/failover-events.jsonl:5` |
| 2026-10-10T09:58:22 | restore snapshot xong: `rpo_seconds` 28.04, `docs_lost` 14, `embed-model=vi-e5-base@v3` (+12.5s) | `reports/failover-events.jsonl:6` |
| 2026-10-10T09:58:25 | health check alert: region-a `UNHEALTHY` sau 3 lần fail liên tiếp (+15.0s) | `reports/health-events.jsonl:2` |
| 2026-10-10T09:58:28 | region-b ready sau warm-up 6.12s (+18.6s) | `reports/failover-events.jsonl:8` |
| 2026-10-10T09:58:28 | DNS cutover `a → b` (+18.6s) | `reports/failover-events.jsonl:9` |
| 2026-10-10T09:58:32 | resolved — request đầu tiên OK từ region-b (+22.5s) | `reports/drill-2-withdr.jsonl:36` |

Ghi chú: runbook xác nhận outage (+12.3s) **trước** alert của health checker (+15.0s), vì
drill khởi động runbook ngay sau lệnh kill. Cutover (+18.6s) vẫn sau alert nên
`tools/measure_rto.py` không có warning.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: 22.5s · gap: còn dư 277.5s (dùng 7.5% ngân sách)
- RPO mục tiêu: 300s · đo được: 28.04s (14 doc bị mất) · gap: còn dư 271.96s
- **Bước tốn nhiều giây nhất:** phát hiện/xác nhận outage — 12.3s trên đường găng (55% RTO);
  health checker riêng là 15.0s. Vì chống flap cần 3 lần fail liên tiếp cách nhau 5s, và với
  netblock mỗi probe phải treo hết timeout 2s mới biết là fail. Tiếp theo là GPU warm-up 6.1s
  và TTL cache của edge 3.9s.
- **Gap ẩn:** RTO 22.5s là số của drill, nơi runbook được chạy ngay tại t_outage. Ngoài đời,
  operator chỉ chạy runbook sau alert (+15.0s), rồi runbook lại probe 3 lần nữa (~12s) →
  RTO thực tế ước tính ≈ 15 + 12.3 + 0.2 + 6.1 + 3.9 ≈ 37.5s. Vẫn PASS, nhưng phần phát hiện
  đang bị tính 2 lần.
- **RPO:** cận trên lý thuyết là chu kỳ replicate 30s; đo được 28.04s. Trong 14 doc mất có
  8 doc ingest trước outage và 6 doc ingest sau outage vào đĩa của region-a.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. *Vì sao user lỗi suốt 22.5s?* — Edge vẫn route vào region-a đã treo cho tới khi có cutover
   và TTL cache hết hạn.
2. *Vì sao edge không tự né region-a?* — Edge không biết health: nó chỉ đọc file
   `edge/active_region` (cache 5s); routing chỉ đổi khi failover ghi file đó.
3. *Vì sao failover mất 12s mới bắt đầu?* — Không có tín hiệu "chết" nhanh: netblock làm
   request treo tới timeout, và luật chống flap đòi 3 lần fail liên tiếp cách 5s.
4. *Vì sao region-b phải restore + warm-up rồi mới serve được?* — B là warm standby rỗng
   (pool `warm`, không data) để tiết kiệm chi phí; state chỉ được snapshot mỗi 30s.
5. *Vì sao mất 14 doc?* — Replication là snapshot bất đồng bộ mỗi 30s, không phải
   stream liên tục; restore dùng bản chụp 17.2s trước outage.

**Root cause:** kiến trúc warm standby + replication snapshot 30s + routing không biết health.
Thời gian phục hồi bị chặn dưới bởi detect floor + warm-up + TTL, và mất dữ liệu bị chặn trên
bởi chu kỳ snapshot.

**Nếu là outage thật, runbook sẽ hỏng ở đâu:**
- Bước 2 `restore_snapshot`: `state/snapshot.py rpo()` đọc DB của region chính để đếm
  `docs_lost`. Nếu region-a mất hẳn, file đó không đọc được → `rpo_seconds`/`docs_lost` = null:
  failover vẫn chạy nhưng ta không biết mất doc nào.
- Bước 1: người trực chỉ biết tin qua alert (+15s), rồi runbook probe lại thêm ~12s (xem gap ẩn).
- Snapshot nằm trong `state/_replica/` trên cùng máy — mất máy/region là mất luôn bản backup.
- Failback chưa có công cụ: `state/replicate.py` chỉ chụp region-a, doc mới ghi vào region-b
  sau failover không được đưa về A.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Runbook nhận alert UNHEALTHY từ health checker làm bằng chứng xác nhận (chỉ probe lại 1 lần), không probe lại 3 lần | On-call SRE | 2026-10-17 | RTO thực tế −10s (≈37.5s → ≈27s) |
| 2 | Hạ health-check interval 5s → 2s, giữ threshold 3, probe từ ≥ 2 điểm (quorum) để không tăng flap | SRE | 2026-10-24 | RTO −6 đến −9s |
| 3 | Edge biết health: upstream timeout thì thử region còn lại; hạ `EDGE_TTL_SECONDS` 5 → 1 | Platform team | 2026-10-24 | RTO −3s |
| 4 | Replication 30s → 5s (hoặc stream WAL/CDC); ghi watermark ingest ra ngoài region-a để vẫn tính được `docs_lost` khi A mất hẳn | Data engineering | 2026-10-31 | RPO từ ≤30s → ≤5s (−23s ở drill này) |
| 5 | Đưa snapshot sang storage khác region (MinIO/S3 CRR) + quy trình failback B → A | Infra lead | 2026-11-07 | Không mất backup khi mất region |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` = 5s × 3 = 15s.** Health checker phát hiện ở +15.0s = 67% của RTO
   22.5s. Trên đường găng của drill, phần xác nhận outage là 12.3s = 55% RTO. Sớm nhất có thể
   phát hiện là (3−1)×5 + 2s timeout ≈ 12s, trễ nhất ≈ 17s. Với RTO 5 phút: các phần còn lại
   ≈ 10.2s (restore 0.2 + warm-up 6.1 + TTL 3.9), nên 3 × interval + 10.2 ≤ 300 →
   interval ≤ ~96s về lý thuyết; thực tế nên giữ 5–10s vì detect floor nên là phần nhỏ của RTO.
2. **Hạ interval xuống 1s:** với netblock mỗi probe treo hết 2s timeout, nên chu kỳ thật ≈ 2s.
   3 lần fail ≈ 6s thay vì 15s → RTO giảm khoảng 6–9s (còn ~14–16s). Cái giá (§4 flapping):
   probe gấp 5 lần; một lần GC pause/nghẽn mạng 3–6s là đủ kích hoạt failover giả. Mỗi lần
   failover giả tốn restore + warm-up + mất tới 30s dữ liệu, và nếu failback cũng tự động thì
   2 region sẽ flap qua lại. Phải hạ timeout < interval và dùng quorum nếu muốn interval ngắn.
3. **Outage 6 giờ, region chính mất dữ liệu vĩnh viễn:** `docs_lost = 14` là 14 ticket khách hàng
   (ingest trong 28s cuối) biến mất vĩnh viễn — RAG không bao giờ trả lời được dựa trên chúng,
   khách phải gửi lại. Con số này bị chặn bởi chu kỳ snapshot, không tăng theo 6 giờ outage,
   *với điều kiện* ingest mới được chuyển sang region-b. Nếu ingest vẫn trỏ vào region-a (như lab
   này) thì mọi doc trong 6 giờ đó cũng mất. Ngoài ra, khi A mất hẳn thì `rpo()` không đọc được
   DB của A, nên ta thậm chí không biết chính xác mất doc nào để báo khách → action item #4.
