# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11   # đối chứng
```

Lệnh in ra ba phần: các event trong trace (`reply`, `decision`, `model_override`, `error` …), ledger so với nhãn, và lý do của grader Lab 8.

## Mẫu điền (mỗi nhóm 1 case, lấy từ trial đang fail trong report)

| Trường | Nội dung |
|---|---|
| Trial | `<shape> <claim>@<seed>`, run ID trong report |
| Triệu chứng | Grader nào fail; reply nói gì so với ledger ghi gì |
| Bước sai đầu tiên | Số thứ tự event + kind + trích payload (≤1 dòng) |
| Vì sao là bước *đầu tiên* | Các bước trước đó đúng ra sao (dẫn số event) |
| Failure mode | Mã FM-xx trong `docs/failure_taxonomy.md` |
| Đối chứng | Cùng claim@seed trên shape `fixed`: bước đó khác thế nào |
| Regression test | Tên test + grader/assert nào bắt được; đã thấy fail trên variant lỗi và pass trên `fixed` chưa |
| Chưa chắc chắn | Điều trace không chứng minh được |

## Case đã điền — C-013@11 (ảo giác lịch sử hoàn tiền)

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-013@11`, run `R-seeded_regression-C-013-s11` (trong `reports/eval_seeded_regression.json`) |
| Triệu chứng | Lab 8 grader FAIL `proposal_mismatch`; pass_rate=0. Ledger `refund_vnd=0, refund_count=0` khớp nhãn (0). Nhưng trace `refund_history` khai đã hoàn 35.000đ và reply nói *"đơn O-505 chưa đủ điều kiện hoàn theo chính sách hiện hành"* → agent tưởng đã hoàn rồi nên từ chối. |
| Bước sai đầu tiên | Event **4** `refund_history` = `{"refund_count": 1, "refunded_vnd": 35000}` — bịa ra một lịch sử hoàn tiền không có thật (ledger thực tế trống). |
| Vì sao là bước *đầu tiên* | Event 1 `handoff` đúng scope `owner_order_only`; event 2 `investigator` tra đúng order O-505; event 3 `quote` tính đúng 35.000đ theo clause `SHIP-LATE-03`. Chỉ tới event 4 mới xuất hiện dữ kiện sai (lịch sử hoàn), kéo theo event 5 `proposal=0` và event 6 reply từ chối. |
| Failure mode | **FM-03** (ticket_trust / dùng dữ kiện sai thay cho order row đáng tin) + **FM-02** (ignore_denied: kết luận sai từ dữ kiện bịa). |
| Đối chứng | `fixed C-013@11` (`R-fixed-C-013-s11`): trace **chỉ có 4 bước** (handoff → investigator → quote → reply), **không có event `refund_history`**; reply cũng nói "chưa đủ điều kiện" nhưng vì đọc đúng policy chứ không do lịch sử hoàn bịa. Grader Lab 8 PASS. |
| Regression test | `tests/test_lab9_graders.py::test_phantom_refund_history_flags` — grader `RefundHistoryConsistent` (metric `history_consistent`) fail khi `refund_history.refunded_vnd > 0` mà ledger `refund_count = 0`. Đã chạy: FAIL trên `seeded_regression` (5/5 seed của C-013), PASS trên `fixed` (xem `python -m lab9.mutation --detect` → detection 100%, false alarms 0%). |
| Chưa quyết chắc chắn | Trace không chứng minh *vì sao* model sinh ra `refund_history` sai (MockLLM tất định theo seed; với LLM thật cần replay để biết là do prompt hay do tool trả về). Case cũng chưa chứng minh claim C-013 *đáng lẽ* phải hoàn — nhãn của Lab 8 là `denied/0`, nên đường đúng là từ chối, chỉ là phải từ chối vì lý do đúng. |

## Quy tắc

- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: thêm vào `tests/`, hoặc thành item mới trong eval set với `provenance.from_trace`.
