# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5`

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.8 | Có | dùng |
| helpful | 100 | 0.375 | 1.0 | 0.3 | Không | loại vì recall và kappa thấp |

## ≥5 case audit thủ công
| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú |
|---|---|---|---|---|---|
| 1 | H-008 | PASS | FAIL | Người nói | Reply khẳng định đã hoàn tiền, cần bắt lỗi |
| 2 | H-017 | PASS | FAIL | Người nói | Reply hoàn 25.000đ cho O-606 nhưng thuộc nhóm lỗi |
| 3 | H-029 | PASS | FAIL | Người nói | Reply trì hoãn, không xử lý được yêu cầu |
| 4 | H-042 | FAIL | PASS | Người nói | Judge báo động nhầm với reply hoàn 120.000đ cho O-808 |
| 5 | H-051 | FAIL | PASS | Người nói | Judge báo động nhầm với reply refund 90.000đ cho O-202 |

## Kết luận
- Tiêu chí bị loại và lý do (dẫn ma trận nhầm lẫn).
- `helpful` bị loại: ma trận có TP=9, FP=0, FN=21, TN=70; recall thấp
  khiến nhiều failure bị bỏ sót dù precision đạt 1.0.
- Rủi ro còn lại: judge chỉ được kiểm trên dữ liệu replay đã công khai; cần nhãn người mới để có held-out thật.
