# Techdy Recruitment Pilot — structured-profile-first

Mở **start.bat**, rồi truy cập **http://127.0.0.1:9652/**. Backend FastAPI, frontend React/TypeScript, PostgreSQL 17. Project ML và demo giáo dục cũ giữ riêng.

## Sử dụng

1. **Tìm ứng viên**: chọn vị trí đã có hoặc mở **+ Thêm JD mới từ PDF**. Không cần Google OAuth để upload cục bộ. JD trùng nguồn Drive mở vị trí đã có, báo rõ file/note chưa thay nguồn; upload cục bộ không đổi nội dung dùng lại dữ liệu đã đọc.
2. Đọc yêu cầu AI đề xuất. Nếu phù hợp, bấm **Dùng yêu cầu này & tiếp tục**. **Chỉnh yêu cầu** là tùy chọn; lưu và xác nhận được gộp vào nút tiếp tục. Điều kiện chưa biểu diễn được đúng giữ UNKNOWN.
3. Xem hướng tìm kiếm rồi **Tìm ứng viên**. BE đối chiếu toàn pool structured và lấy Top20; AI tự đánh giá một nhóm, không đọc lại PDF hoặc gửi từng full CV. Có thể đổi số người gửi AI (1–20) trước khi tìm.
4. Mặc định hiển thị Top10 theo điểm backend tính sau AI. Chọn 6/7/8… trong **Hiển thị** không gọi AI lại, không mất tick. Xem score/evidence và trạng thái từng người. Tick người rồi **So sánh sâu & Recruitability** là bước AI riêng.
5. **Ghi đánh giá**, xem lịch sử hoặc mở **Xuất kết quả và đối chiếu chất lượng**. CSV/Sheet theo TopK có cả người cần xác minh. Sheet live cần OAuth; benchmark tách retrieval recall và AI ranking, không tự đổi policy hoặc tạo nhãn.

## Vận hành và cập nhật

- Mở ứng dụng: **start-pilot.cmd**.
- Kiểm tra hệ thống và code: **check-pilot.cmd** (không gọi AI; có build lại giao diện).
- Sao lưu database và thử khôi phục vào database riêng: **backup-pilot.cmd**. File nguồn/config/token cần giữ riêng khi chuyển máy.
- Quy trình nâng cấp, khôi phục, bản đồ module và các điều kiện mở rộng: [docs/START-HERE.md](../docs/START-HERE.md). Những thay đổi gần nhất: [docs/START-HERE.md](../docs/START-HERE.md).

## Policy và strategy

Policy theo job, có version trong config/run: gate ALL_MET / NO_CONFIRMED_FAILURE / REVIEW_ONLY; threshold bật/tắt, giá trị, toán tử > hoặc >=; trọng số MUST/NICE/OPTIONAL; alpha và TopK.

Preset 3/1/0,5, mọi MUST MET, điểm >85 là **preset chưa benchmark**, không phải business truth. MET/PARTIAL/NOT_MET nhận 100/50/0% trọng số. UNKNOWN không nhận điểm đã xác nhận nhưng giữ trạng thái riêng, không chuyển thành NOT_MET. Điểm T có denominator là toàn bộ trọng số criterion bật; coverage cho biết phần evidence đã xác nhận. Lỗi đọc/parse có trạng thái riêng.

Mỗi strategy chứa predicates trên fact schema, ALL/ANY và trọng số. Text so khớp tên fact chuẩn cùng bảng alias có version. Có operator at_least để kiểm tra ít nhất N giá trị khác nhau, ví dụ 2/3 ngành. Tín hiệu thiếu/nguồn mâu thuẫn giữ UNKNOWN.

S là strategy fit cao nhất; R = (1−alpha)T + alpha*S. Khi tắt mọi strategy, R=T. BE chọn nhóm đầu vào theo R, không dùng MUST/threshold để prefilter trước AI. Sau batch, backend tính lại T từ AI criterion statuses và áp dụng policy để gắn trạng thái; TopK vẫn hiển thị mọi trạng thái. Thứ hạng cuối: T, coverage, BE rank, candidate ID. Chi tiết triển khai/kiểm thử: [docs/START-HERE.md](../docs/START-HERE.md).

## PostgreSQL và dữ liệu

Máy dùng cluster riêng tại runtime/postgres-data, chỉ bind 127.0.0.1:55432, database recruitment_pilot. Không sửa cluster PostgreSQL 17 hiện có ở cổng khác. setup-postgres.ps1 khởi động cluster và thực hiện migration idempotent. DATABASE_URL có thể cấu hình ở backend nếu dùng database pilot riêng khác.

SQLAlchemy/psycopg kết nối; Alembic quản lý schema. SQLite gốc và before-structured-migration.sqlite3 được giữ. Engine cũ không được resume. Source, profile và config có revision; run snapshot giữ policy và candidate revision. Fact evidence nằm trong immutable profile revisions, Round 2 chỉ lấy revisions của nhóm đã tick.

CV/note mới hoặc thay đổi mới gọi AI extraction. File không đổi không parse lại. Backfill schema là tác vụ riêng có dự toán tại Kết nối & prompt; thêm JD không kích hoạt backfill CV. Fact sửa bởi recruiter có lịch sử, kiểm tra quote và được giữ khi sync; nguồn đổi khiến bản sửa cần review trước khi trở lại pool READY.

Secrets, source text, dữ liệu cá nhân, uploads, token, database và backups nằm trong runtime/ đã bỏ khỏi Git. Không đưa runtime lên public hosting. Loopback Host/Origin/header guards vẫn hoạt động. Bản này cho một khách trên localhost, chưa có phân quyền nhiều khách.

## Provider và Google

Khách chọn DeepSeek/Gemini/Claude/OpenAI, model và đường dẫn file API key trong Kết nối & prompt. Backend đọc DEEPSEEK_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY. Không nhập key vào frontend/Git/log. Đổi provider không tự parse lại CV; không fallback âm thầm. Provider khác DeepSeek cần key khách để kiểm chứng live.

Google cần OAuth Desktop JSON, Drive API và Sheets API. Token lưu ngoài Git. Sau kết nối, Drive kiểm tra mỗi 120 giây và khi khởi động; có Đồng bộ ngay. Local uploads không bị đánh dấu mất nguồn khi quét Drive. Snapshot connector là dữ liệu thật tại lúc nạp, không phải live sync. Hiện chưa có OAuth Desktop token; upload JD/CSV vẫn dùng được.

## LLM calls và benchmark

- 1 extraction/candidate revision gồm CV+note.
- JD mới: 1 chuẩn hóa + 1 criteria + 1 strategies sau duyệt.
- Structured retrieval, gates, strategy ranking, đổi số hiển thị TopK và Sheet: **0 calls**.
- Luồng tìm hiện tại thêm **1 batch AI assessment** cho Top20 sau retrieval; backend tính điểm cuối.
- Comparison: 1 batch nhóm tick khi vừa context budget. Quá 100.000 ký tự báo giảm nhóm trước khi gửi.
- Retry tối đa 3 lần thử tổng, tối đa 2 AI calls đồng thời. Log token do provider trả, model, prompt version/hash. Usage database kiểm thử riêng không gộp vào dashboard production.

Pool đã sẵn sàng: 30/1.000/2.000 CV đều dự kiến **4 calls** cho JD mới đến kết quả batch: chuẩn hóa JD + criteria + strategies + assessment. Comparison riêng thêm 1 call. Lần đầu gồm pool + JD + batch + comparison dự kiến 35/1.005/2.005 calls, chưa tính retry hoặc sinh lại chủ động. Số call không phải cam kết latency/chi phí; cần đo usage thực.

Lịch sử & feedback → Benchmark. Nhãn GOOD/NOT_GOOD/UNCERTAIN theo job và profile revision. UNCERTAIN, chưa review hoặc stale không là negative. So sánh gate, threshold, alpha, K; báo recall, precision trên phần có nhãn, người tốt bị bỏ sót và leave-one-job-out khi đủ jobs. Không tự thay policy. Hiện chưa có labeled data đủ để chốt accuracy/preset.

## Kiểm thử và backup

```powershell
.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend run build
.venv\Scripts\python.exe -m backend.backup --verify
```

Backup tạo dump trong runtime/backups; --verify khôi phục vào database kiểm tra mới, không ghi đè production. Đã kiểm chứng counts files/profiles/facts/runs/calls. Để chuyển máy, backup database và các file runtime cần thiết; bảo vệ secrets/token riêng.

Xóa file trên Drive chỉ đánh dấu source không khả dụng, vẫn giữ lịch sử. Đây không phải xóa toàn bộ dữ liệu cá nhân: khi cần purge, phải xử lý profile/source/history, backups và các Sheet đã xuất; không tự xóa những bản khách đã review.

backend.validate_structured gọi AI thật trên dữ liệu thật trong database kiểm thử riêng, có phát sinh token. Load clones chỉ nằm trong database kiểm thử; không xuất hiện trong pool khách. Xem VERIFICATION.md cho số đo và các giới hạn.

## Đánh giá một CV với JD

Mở **Tìm ứng viên → Mở yêu cầu & tìm CV** cho vị trí mong muốn. Tại mục **CV này có phù hợp với job?**, chọn CV PDF rồi bấm **Đánh giá CV với job này**. Có thể thêm private note trước khi gửi. Yêu cầu JD cần được khách xác nhận; đánh giá riêng không cần duyệt scouting strategies.

CV có chữ, tối đa 15 MB; bản scan báo cần OCR, không tự cho điểm 0. CV được lưu vào candidate pool. Lần đầu chuẩn hóa có 1 call AI, sau đó 1 batch assessment chỉ gồm CV này; cùng hồ sơ và input được tái sử dụng. Không chạy toàn candidate pool. Model/provider theo lựa chọn đang cấu hình, không fallback âm thầm. Kết quả gồm điểm, độ phủ, điều kiện hiện tại, bằng chứng và câu hỏi; chưa đủ thông tin giữ UNKNOWN. Đây là hỗ trợ recruiter, không quyết định tuyển dụng tự động.

REST API: `POST /api/v1/jobs/{job_id}/cv-assessments` (multipart file/config_version/private_note), `GET /api/v1/cv-assessments/{ticket_id}`, `POST /api/v1/cv-assessments/{ticket_id}/retry`. Khi chuẩn hóa xong, ticket trả search_id; đọc/stream kết quả bằng API search hiện có. Retry search giữ scope single-cv và tối đa 3 lần thử tổng.


## Exa web search

Mục **Tìm người trên web** hỗ trợ Web/People, lưu lịch sử và phí Exa trả về. Trong JD → Hướng tìm kiếm, mỗi strategy có truy vấn Exa chỉnh sửa được. Soạn truy vấn cũ theo một batch, lưu/duyệt lại rồi chọn strategy để chạy People. Chỉ truy vấn đã chọn được gửi đi; kết quả là nguồn công khai chưa được đánh giá phù hợp JD. Xem [bản đồ code hiện tại](../docs/START-HERE.md).
