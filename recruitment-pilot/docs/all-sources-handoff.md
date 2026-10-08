# Xử lý toàn bộ nguồn công khai — 2026-10-08

## Dùng trên pilot tiếng Việt

1. Mở http://127.0.0.1:9652/, chọn **Tìm người trên web**.
2. Mở JD đã xác nhận, vào **Hướng tìm kiếm**, mở nhóm People đã tìm trong lịch sử.
3. Đọc số URL đã gom trùng và preview cache. Preview không gọi Exa/AI.
4. Bấm **Lấy nội dung & đánh giá tất cả N nguồn — có phí** để xác nhận một lần.
5. Theo dõi số có nội dung và đã chấm. Kết quả từng batch chỉ xuất hiện sau validate.
6. Đổi TopK để xem kết quả đã lưu; không gọi AI lại.

Nguồn thiếu text không tự dùng highlights. Nguồn quá dài để chấm riêng, hoặc batch lỗi,
vẫn có trong danh sách chưa chấm. Trạng thái PARTIAL không phải hoàn tất toàn bộ.
Phần chấm nhóm riêng bên dưới giữ khả năng bổ sung PDF, chủ động chấm đoạn trích và
xem lịch sử cũ. Không nhập nguồn vào kho CV, không liên hệ người được tìm.

## Kiến trúc và khôi phục

- `backend/assessment_groups.py`: snapshot, fingerprint, điều phối tuần tự, packing và hợp nhất.
- `backend/assessment_group_api.py`: REST routes; route không chứa phép chấm điểm.
- Migration `0009_assessment_groups`: bảng cha; JSON ledger liên kết các ID con đã lưu.
- Lấy nội dung theo lượt tối đa 20 URL, qua adapter `/contents` hiện có; cache 24 giờ
  tính theo thời điểm lấy nội dung gốc, không gia hạn chỉ vì dùng lại.
- Sau lấy nội dung, manifest nội bộ giữ revision của nguồn thành công. Manifest không
  gọi provider. AI packing qua ranh giới lượt lấy nội dung, thứ tự nguồn ổn định, tối đa
  20 và vừa context/output/cells. Không cắt hoặc tóm tắt nội dung để ép vừa.
- Dùng validator và scorer hiện có. URL là định danh qua batch; không gộp theo tên.
- Sắp theo điểm, coverage, thứ tự nguồn ban đầu và ID. Không có call rerank toàn nhóm.
- Khi restart, tác vụ cha đang chạy được tiếp tục; con COMPLETED không chạy lại.
- Retry phần lỗi chỉ dùng số lần còn lại trong tổng ba lần mỗi con. Hết lượt thì báo rõ.
- JD/config/model/prompt/budget/scorer đổi sẽ chặn tiếp tục, giữ lịch sử. Không trộn cấu hình.
- Phí lấy nội dung cộng từ usage của các lượt con, có nhãn chưa xác định. Usage AI gồm
  các lần thử. Chi phí lượt con được dùng lại không phải phí mới. Không tự suy tiền từ token.

## API

- POST `/api/v1/people-searches/{id}/assessment-groups` `{config_version}` → 202 + ID.
- GET `/api/v1/people-searches/{id}/assessment-groups` → lịch sử.
- GET `/api/v1/people-searches/{id}/assessment-group-preview?config_version=...`
  → đọc cache/budget cục bộ, không tạo tác vụ có phí.
- GET `/api/v1/assessment-groups/{id}` → tiến trình, kết quả, chưa chấm, usage và phí.
- GET `/api/v1/assessment-groups/{id}/results?limit=7` → kết quả đã lưu.
- POST `/api/v1/assessment-groups/{id}/retry` → thử phần lỗi còn lượt, giữ batch thành công.
- GET `/api/v1/assessment-groups/{id}/sources/{source_id}` → nội dung và revision đã chấm.

API chấm nhóm cũ và lịch sử cũ được giữ. Chi phí/latency thực của lượt xử lý tất cả chỉ
được đo sau khi người dùng bấm xác nhận; mock tests không chứng minh chất lượng tuyển dụng.

## Bằng chứng kiểm chứng

- Có bộ kiểm thử 10/20/38 nguồn, packing toàn nhóm, URL trùng, missing text, singleton
  quá dài, preview không có call, TTL cache, retry, input drift và restart trên DB test riêng.
- Backup trước migration: `runtime/backups/pilot-20261008T014141Z.dump`.
- Restore và migration đã thử trên DB riêng `pilot_restore_validation_20261008014143`.
- 10 lượt public assessment lịch sử được đối chiếu snapshot/data/status/attempts nguyên vẹn.
- Nhóm People `84241c9f-95d9-4e07-8207-8b69824d12d8`: 38 URL, preview lúc kiểm tra
  có 11 text trong cache, 27 cần lấy. Đây là số tại thời điểm kiểm tra, cache sẽ hết hạn.
- Browser kiểm chứng nút xử lý 38 nguồn sẵn sàng; không bấm nút tạo lượt có phí.
- `runtime/verification/` giữ preview, kết quả kiểm thử và ảnh browser.

Độ ổn định điểm giữa các batch và chất lượng tuyển dụng vẫn cần nhãn khách xác nhận.
Không cam kết 100% LinkedIn, độ phù hợp 100%, hay thời gian dưới hai phút khi chưa đo live.
