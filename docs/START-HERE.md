# Bàn giao cho người học và GPT tech lead

Tài liệu này được viết ngày 2026-10-06 từ source hiện tại. Đây là điểm bắt đầu; khi có khác biệt, đọc code và test để xác minh. Không giả định tài liệu cũ hoặc cuộc trò chuyện đã compact mô tả đúng phiên bản đang chạy.

## Người học và cách dạy

Chủ project muốn hiểu code để tự sửa bug và hỗ trợ khách. Dạy bằng tiếng Việt, giải thích thuật ngữ mới. Người học hiểu sơ bộ retrieval lấy Top20, AI đánh giá nhóm, TopK và việc giữ UNKNOWN. Chưa tự tin với ingestion, cache, revision và invalidation.

Mỗi lượt chỉ một bài nhỏ: nguyên lý → yêu cầu tìm hàm/file thật → người học giải thích → đánh giá câu trả lời → sửa một tình huống trên dữ liệu giả → chạy test liên quan. Không đưa toàn bộ đáp án ngay, không hỏi dồn. Phân biệt câu hỏi lý thuyết và câu hỏi về implementation đã xác minh. Khi chưa đọc code, nói rõ chưa xác minh.

Không hứa 'hiểu 100%' sau một checklist. Tiêu chuẩn thực hành: người học lần theo được request, chỉ ra nguồn dữ liệu, tái hiện bug, viết test có ý nghĩa và kiểm chứng cách sửa.

## Bài toán và giới hạn

Ứng dụng hỗ trợ recruiter: nhập CV/JD và private note; chuẩn hóa có bằng chứng; khách xác nhận criteria và scouting strategies; tìm ứng viên; AI đánh giá nhóm nhỏ; khách chọn người để so sánh sâu/Recruitability; xuất kết quả. Không quyết định tuyển dụng hay liên hệ người tự động.

Hai ứng dụng khác nhau:

- `recruitment-pilot`: backend thật và UI tiếng Việt, port 9652, PostgreSQL. Gọi provider thật khi có cấu hình và thao tác tương ứng.
- `portfolio-en`: frontend tiếng Anh port 8654, hồ sơ giả và điểm preset; không gọi AI, không parse CV thật.

Repo chỉ chứa source. Database/history/uploads/key/token/ảnh/video khách được giữ cục bộ, không nằm trong Git. GPT khác muốn xem private repo cần được cấp quyền đọc hoặc nhận source ZIP riêng; chỉ gửi link không tự cấp quyền.

## Luồng hiện tại

### 1. Ingestion

Drive inventory hoặc upload → trích văn bản → kiểm tra thay đổi → profile có cấu trúc → validate bằng Pydantic + evidence → lưu profile/facts/revisions.

Đọc `engine.ingest`: file ID và modified time giúp phát hiện khả năng thay đổi; hash hiện được tính từ **văn bản đã trích xuất**, không phải mặc định hash bytes PDF. File đổi modified time nhưng cùng text hash không nhất thiết tạo profile mới. Còn mapping/group/availability cũng có thể làm nguồn bị coi là thay đổi. File không đổi có fast path không đọc lại. Khi parsing lỗi, có trạng thái riêng, không biến thành điểm 0.

`engine.invalidate` và phần cuối `ingest` xử lý trạng thái stale; thay candidate có thể khiến các run tìm trên toàn pool thành stale vì tập input đổi. Không xóa lịch sử hay âm thầm thay snapshot đã review. JD mới không được backfill cả kho CV không đổi. Backfill schema là thao tác riêng.

### 2. JD → criteria → strategies

JD được chuẩn hóa một lần theo revision. AI đề xuất criteria. Recruiter sửa và xác nhận. AI sinh strategies có rule nội bộ và `exa_query` trong một call. Version/hash theo dõi việc strategy còn tương ứng với criteria không. Sửa query cần lưu config mới và duyệt lại. Người học cần phân biệt lưu, xác nhận criteria, sinh strategy, duyệt toàn config.

### 3. Structured retrieval → batch assessment → TopK

`search_api` nhận config version → `search_orchestration.create` → `engine.create_run` → structured retrieval toàn pool → chọn tối đa 20 hồ sơ.

Retrieval không gọi LLM, không đọc lại PDF. Rule ALL/ANY, alias Go/Golang và evidence quyết định assessment; strategy ảnh hưởng retrieval rank. Trong workflow batch, không prefilter bằng MUST/threshold trước AI.

Snapshot giữ JD/config/candidate revisions. `ai_assessment` tạo payload gọn gồm facts và evidence catalog; không gửi điểm hoặc thứ hạng BE cho model. Model trả từng criterion status và evidence reference, không tự tính điểm tổng. Backend kiểm tra IDs, schema, bằng chứng và tính điểm bằng `assessment_scoring`/`retrieval`.

UNKNOWN giữ riêng với NOT_MET. Điểm đã xác nhận và coverage khác nhau. Công thức hiện có denominator theo tổng trọng số bật; thiếu evidence có thể hạ điểm đã xác nhận nhưng không phải chứng minh thiếu năng lực. MUST/threshold là policy configurable, chưa được coi là business truth hoặc benchmark accuracy.

Thứ hạng cuối: điểm chuyên môn, coverage, BE rank, candidate ID. Đổi Top10 thành Top7 đọc kết quả lưu, không gọi AI lại. Streaming trong `judge_stream`/API cho progress; các preview chưa được validate đầy đủ không được xem là kết quả cuối. Đọc implementation trước khi mô tả đảm bảo thời gian thực hoặc latency.

### 4. Comparison và một CV

Comparison/Recruitability là bước riêng cho IDs khách tick. Recruitability đối chiếu mong muốn đã có evidence; không suy ra xác suất nhận offer từ tenure/title hoặc đặc điểm nhạy cảm.

`single_cv` nhận một CV cho một JD đã xác nhận, lưu ingestion ticket rồi đánh giá chỉ ứng viên đó. Nó tái sử dụng assessment/scoring; không đánh giá lại cả pool. Lần đầu có thể gồm extraction + assessment; retry phải giữ scope.

### 5. Exa People

Trong workspace JD: strategy + query → recruiter sửa/duyệt → chọn hướng → People search group.

`people_search` chụp snapshot và tạo child request cho từng query được chọn. `exa_search` gửi `/search`, `category=people`, `type=auto`, highlights. Web mode cũ mặc định web. Tối đa 10 nguồn/query; không fallback mode/domain/date filters.

Gom URL trùng giữ mọi strategy provenance; không gộp tên người. Cache 24h phân biệt query/mode/adapter; làm mới chủ động mới tạo request mới. Phí chỉ ghi theo provider; một lượt có phí chưa biết không được gọi là miễn phí. Một child lỗi không làm mất child thành công. Retry tối đa ba lần tổng. Query guard là hỗ trợ; không bảo đảm mọi free text đã được khử private data, recruiter phải review.

Kết quả tìm là nguồn hồ sơ công khai **chưa đánh giá phù hợp JD**. Recruiter có thể chọn JD đã xác nhận và chủ động bấm đánh giá nhóm nguồn. Module `public_assessment` chụp JD/config/public excerpts, gửi một batch tới provider được chọn, kiểm tra IDs/evidence refs và tính điểm/coverage ở backend. UNKNOWN giữ riêng; đây là đánh giá sơ bộ từ đoạn trích, không phải CV hay danh tính đã xác minh. Không tự import, enrich hoặc liên hệ.

Trang tìm web có upload JD PDF dẫn vào luồng xác nhận yêu cầu hiện có. Ô query cho soạn đến 30.000 ký tự, báo giới hạn 1.500 ký tự gửi tìm kiếm và không bị khóa khi lượt trước chạy. Dán nguyên JD thì dùng upload và rút gọn query; không tự cắt nội dung. Kết quả đánh giá lưu theo snapshot và tìm lại được khi mở cùng search/JD. Đổi số hiển thị không gọi AI lại. API `/api/v1/public-assessments` hỗ trợ tạo, đọc lịch sử, trạng thái và retry tối đa ba lần tổng. Budget model phải được xác nhận; không tự đổi provider hoặc chia calls.

## Bản đồ code (đường dẫn tính từ recruitment-pilot)

| File | Nắm gì |
|---|---|
| frontend/src/main.tsx | State/navigate/load/action và nối UI với API |
| frontend/src/request.ts | Request helper, localhost mutation header, lỗi HTTP |
| frontend/src/JobWorkspace.tsx | Criteria, strategies, bước xác nhận/tìm |
| frontend/src/PeopleScouting.tsx | Query editor, selection, polling/history People |
| frontend/src/SearchResults.tsx, BatchSearch.tsx | Kết quả assessment, TopK, tick và progress |
| frontend/src/SingleCVUpload.tsx | CV cho JD riêng |
| backend/api.py | Lifespan, guards, route cũ, upload, criteria/settings |
| backend/search_api.py, people_api.py, exa_api.py | Các router mới; routes delegate service |
| backend/engine.py | Ingestion, normalization, criteria/strategy, queue, invalidation, comparison |
| backend/models.py, search_schema.py | Pydantic schema và trạng thái hợp lệ |
| backend/retrieval.py | Rule/alias/evidence/coverage/strategy/policy |
| backend/search_orchestration.py | Chọn nhóm, snapshot/cache, chạy assessment, lưu kết quả |
| backend/ai_assessment.py, assessment_scoring.py | Payload/validation và điểm sau AI |
| backend/ai_runtime.py, providers.py, prompts.py | Provider adapters, retry/budget/usage và prompt |
| backend/judge_stream.py | Đọc response streaming/JSON từng phần |
| backend/integrations.py, sheets.py | Drive/OAuth/text extraction và Sheet |
| backend/people_search.py, exa_search.py | People groups và provider search |
| backend/db.py, postgres.py | DB wrapper, config, init và restart recovery |
| migrations/versions | 0001 structured, 0002 batch, 0003 Exa, 0004 People, 0005 public assessment |
| tests | Dữ liệu giả, provider mock, regression theo module |

Modular monolith từng bước: chưa refactor sạch toàn hệ thống. `engine.py`, `api.py`, `main.tsx` còn nhiều trách nhiệm; route cũ và mới cùng tồn tại. Không dạy rằng kiến trúc đã hoàn toàn decoupled hoặc RESTful chuẩn ở mọi endpoint.

## Học code theo thứ tự

1. Lần theo click Tìm ứng viên: main → request → search_api → service; ghi input/output.
2. Đọc một test và giải thích fixture/mock/assert khác nhau thế nào.
3. `models` và một profile giả: phân biệt skill, evidence, revision.
4. `ingest`, hash và stale: làm test nguồn không đổi/đổi text.
5. Retrieval: ALL/ANY, Go/Golang, UNKNOWN; chỉnh fixture, đo kết quả.
6. Snapshot/cache/idempotency: bấm lại, sửa input, restart khác nhau thế nào.
7. AI payload: chỉ ra chính xác dữ liệu gửi provider; evidence ID được resolve ở đâu.
8. Điểm cuối/coverage/TopK: tính tay một ví dụ rồi đối chiếu test.
9. Async queue/stream/retry: tìm lý do UI pending, FAILED, INTERRUPTED.
10. Single CV và comparison: bảo đảm selected IDs/scope.
11. Exa strategy groups: mode, snapshot, provenance, cache và phí.
12. Vận hành: startup, migration, backup/restore và kiểm chứng rollback.

Không sửa code production khi làm bài học. Tạo branch/test fixture; không approve hoặc tạo nhãn hộ khách.

## Bản đồ xử lý bug

| Triệu chứng | Điểm bắt đầu kiểm tra |
|---|---|
| Nút disabled | Frontend dirty/busy/task/current/approved; so config version với server |
| Upload HTTP 409 | IntegrationError detail, duplicate/mapping/config/current; không coi mọi 409 là crash |
| Điểm lạ | criterion statuses/weights/evidence/coverage và scorer version |
| AI JSON lỗi | finish reason/output budget/schema/IDs/evidence; không tăng retry vô hạn |
| Chạy lâu | queue pending hay provider đang streaming, attempt count/usage; không khẳng định phần cứng khi chưa đo |
| CV mới chưa xuất hiện | OAuth/last_sync, folder mapping, file status/hash/profile READY |
| Exa trống/lỗi một hướng | selected/enabled/query/version, child status, provider error và phí chưa xác định |
| Restart mất hoặc trùng task | db.init recovery, transaction/enqueue và fingerprint |

Luôn xác định tầng gây lỗi, tạo reproduction, kiểm tra regression liên quan rồi mới kết luận fix. Phân biệt test giả, live test trên DB riêng và thao tác thực trên dữ liệu khách.

## Calls, dữ liệu và bàn giao

Pool đã chuẩn hóa: thêm một JD đến kết quả batch thường 1 normalization + 1 criteria + 1 strategies + 1 batch assessment = 4 successful calls, với pool 30/1000/2000 như nhau. Extraction CV mới/đổi, comparison, soạn lại query và retry là call thêm. Exa là request provider riêng, không được gộp thành LLM call. Giá/latency phải đo usage thực.

Test đang có 104 cases tại lần kiểm chứng trước bản bàn giao; lần kiểm chứng mới phải tự chạy, không dùng con số này làm bằng chứng vĩnh viễn. Nhãn khách/benchmark recall và ranking vẫn là hạng mục cần kiểm chứng, không công bố accuracy chưa có nhãn.

Runtime không có trong repo, do đó clone không mang theo trạng thái customer/OAuth hoặc database thật. UI localhost không phải sản phẩm cloud multi-tenant. Trước bàn giao online cần thiết kế auth, access control, privacy, backup/restore, monitoring và deployment riêng.

## Prompt gửi GPT khác

"Hãy đọc README.md và docs/START-HERE.md rồi kiểm tra source trước khi dạy. Bạn đóng vai tech lead hướng dẫn tôi hiểu project để tự sửa bug. Dạy tiếng Việt, một bài và một câu hỏi mỗi lượt. Bắt tôi tìm hàm thật, giải thích input/output, rồi làm bài code/test trên dữ liệu giả. Phân biệt retrieval score và điểm sau AI, structured profile và embedding, UNKNOWN và NOT_MET, real pilot và portfolio giả. Không approve JD/label hộ khách hoặc gọi API trả phí khi chưa có yêu cầu. Bắt đầu bằng luồng click Tìm ứng viên và chờ tôi trả lời."
