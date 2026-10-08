# Hướng dẫn AI tìm người theo JD

1. Mở **Tìm người trên web**, chọn JD, vào **Hướng tìm kiếm**.
2. Sửa **Hướng dẫn AI tìm người**, bấm **Lưu hướng dẫn**. Lưu không gọi AI hoặc Exa.
3. Bấm **Sinh lại hướng tìm kiếm** (có phí AI). Hoặc **Soạn truy vấn Exa** để giữ nguyên chiến lược, chỉ viết lại truy vấn.
4. Kiểm tra mọi MUST và truy vấn cuối, sửa nếu cần, duyệt config mới rồi chủ động tìm nguồn.

Khôi phục mặc định chỉ đổi ô nháp. Bấm Lưu mới tạo phiên bản. Hướng dẫn mỗi JD độc lập. Nếu tab khác đã lưu, hệ thống báo 409 và giữ nháp; tải bản hiện hành, đối chiếu rồi lưu tiếp.

Hướng dẫn được chụp vào tác vụ cùng prompt/hash. Lượt đang chạy và lịch sử không thay đổi khi sửa hướng dẫn. Config mới chưa duyệt; không tự tìm Exa. Hướng dẫn chỉ điều chỉnh nghiệp vụ scouting, không đổi JSON, criteria, MUST, công thức điểm hoặc validator.

Ví dụ: Ưu tiên người hiện giữ vị trí Phó Tổng Giám đốc hoặc Head; không coi Vice President là chức danh tương đương. Đây là sở thích tìm kiếm, không tự biến thành tiêu chí loại hồ sơ trong kho CV.

Kiểm chứng: kiểm thử hồi quy và 13 kiểm thử hướng dẫn, build frontend, lưu/reset/reload và hai tab xung đột. Backup trước migration và restore trên DB riêng đã kiểm tra. Ba lượt AI thật đã được xác nhận và hoàn tất, tạo config Sitecore 8 và Phó Tổng 5 chưa duyệt, không tìm Exa.

Giới hạn đã phát hiện: lượt Phó Tổng đầu tiên chưa diễn đạt rõ ưu tiên chức danh. Sau sửa mâu thuẫn prompt và thêm hướng dẫn vào INPUT, lượt bổ sung đã đưa ưu tiên Phó Tổng/Head vào cả 5 mô tả, nhưng truy vấn Exa vẫn tổng quát. Prompt tiếp tục được làm rõ để mỗi truy vấn tự mang ưu tiên nghiệp vụ; thay đổi cuối chưa chạy AI thật. Chưa nghiệm thu chất lượng truy vấn, cần recruiter xem output trước khi duyệt.
