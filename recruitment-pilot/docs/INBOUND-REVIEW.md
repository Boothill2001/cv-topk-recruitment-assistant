# Jev inbound: kiểm chứng tập CV tại localhost

Luồng này chọn trực tiếp 1–20 CV đã nhập, không chạy retrieval Top20 cũ và không đo recall toàn kho. Bản online từ chối các API inbound này.

## Cách dùng

1. Mở localhost, vào **Tìm ứng viên**. Tại JD có criteria đã duyệt, chọn **Kiểm chứng Jev trong kho CV**.
2. Chọn rõ 1–20 CV. Có thể tìm theo mã/tên file; hồ sơ thiếu text vẫn được theo dõi. Trùng hash giữa các ID chỉ cảnh báo, không tự gộp.
3. Bấm preview: kiểm tra revision, rubric, các phần/request, phí dự phòng và ledger. Preview không gọi AI/Exa.
4. Đóng băng tập kiểm chứng. JD/config, model/adapter/scorer và nội dung nguồn được lưu theo revision/hash. Không tự chạy AI.
5. Review độc lập từng CV với nhãn đáng liên hệ/cần xác minh/không phù hợp và lý do. Điểm, kết quả AI và trace kết quả ẩn trước khi mở điểm. Nhãn lưu riêng, có phiên bản và chống ghi đè tab khác.
6. Preview và xác nhận gửi tập CV tới TypeSafe/Jev. Chỉ text candidate_public hợp lệ được gửi; không đọc lại PDF, không gửi private note, không gọi Exa. Hạn mức dùng chung ledger cũ. Điều chỉnh hạn mức là thao tác xác nhận riêng, không topup provider và không chạy AI.
7. Khi đã có kết quả validate và đã review đủ tập, mở điểm để đối chiếu. Điểm là mức đáp ứng có bằng chứng; chưa phải chất lượng shortlist đã nghiệm thu.
8. Xem từng quote/file/revision/vị trí, lưu phản hồi evidence riêng, mở trace/artifact và tải TXT/JSON. Đổi TopK, trace và export chỉ đọc dữ liệu lưu. JSON chứa dữ liệu CV/JD: chỉ chia sẻ người có quyền.
9. Với PARTIAL, xem lý do từng CV rồi preview phần chưa hoàn tất. Tiếp tục dùng cùng snapshot, tái sử dụng bước đã validate. Request chưa rõ kết quả/phí hoặc lỗi key/model/schema phải kiểm tra, không tự gửi lại. Hồ sơ thiếu text/revision hoặc rubric cần review vẫn chưa hoàn tất.

## Năm điểm yếu đã xử lý

| Điểm yếu | Cách xử lý trong luồng mới |
|---|---|
| Retrieval cũ có thể bỏ CV phù hợp trước khi Jev thấy | Chọn tập CV trực tiếp, không gọi create_run để lọc trước |
| Bằng chứng bị mất/lẫn do text dài hoặc đoạn lặp | Planner chia phần, giữ đoạn lặp, vị trí gốc/file/revision; hash và offset phải đúng |
| Người review bị điểm AI dẫn dắt | Nhãn độc lập, ẩn điểm/kết quả/trace trước reveal, nhãn BEFORE/AFTER tách riêng |
| Lỗi/restart có thể chạy lại việc đã tính phí | Dùng queue/artifacts/ledger hiện có; lưu từng nguồn/bước, chặn UNCERTAIN và chỉ tiếp tục phần thiếu |
| Demo kỹ thuật bị hiểu thành chất lượng tuyển dụng | Gắn nhãn mô phỏng cho QA; báo số đã/chưa chấm và đối chiếu nhãn trong tập, không tuyên bố recall toàn kho |

## Lưu trữ và tương thích

Migration 0014_inbound_review bổ sung review sets/labels; không tạo queue hoặc ledger khác. Text dài nằm một lần trong artifacts theo hash. Các endpoint /api/v1/jobs/{id}/inbound-review-sets và /api/v1/inbound-review-sets/{id} dùng storage/worker/scorer hiện có. API cũ vẫn tương thích. Chỉ sau reveal mới tạo result run phục vụ review evidence cũ. Chưa hỗ trợ nhập JSON bên ngoài.

## Giới hạn

- Tối đa 20 CV trong một tập thử. Đây chưa phải hệ thống tìm toàn bộ kho CV lớn.
- Chưa tự nhập/sync Drive, chưa gửi CV lên Botkeep. CV không có text hợp lệ cần xử lý nguồn riêng.
- Rubric không bảo toàn được ý nghĩa criteria thì giữ chờ review. Không tự diễn giải để chạy cho hết.
- Chỉ số trong tập review không đại diện toàn thị trường hoặc toàn kho; cần anh Đạt review nghiệp vụ và chốt ngưỡng cho từng JD.
- Kiểm thử mô phỏng không chứng minh provider/model/key thật hiện hoạt động hoặc thứ hạng tốt. Lượt thật cần tập CV cụ thể, quyền sử dụng dữ liệu và xác nhận phí.
