# Changelog

## 3.0.4 — 2026-09-20

### Fixed
- `html_font.apply()` (bản sao từ framework): chỉ trỏ stack `-apple-system,…` về token khi nó nằm trong khối `<style>` của `<head>`. Bản 3.0.3 quét regex trên toàn trang nên có thể cắt đôi stack chứa `"Segoe UI"`, ăn mất nháy đóng của chuỗi JS, và đụng vào tài liệu con trong `srcdoc`. Nhận `</HEAD>` không phân biệt hoa thường; stack `monospace` không bị đụng.

## 3.0.3 — 2026-09-20

### Changed
- Mọi trang HTML engine sinh ra (graph, atlas) dùng font mặc định **Lexend Deca Light** (weight 300 cho nội dung, trục 300–700 cho chữ đậm/tiêu đề), NHÚNG base64 (~49 KB/trang, subset latin + tiếng Việt) — mở `file://` không mạng vẫn đúng font, không gọi Google Fonts. `engine/html_font.py` + `html_font_data.py` là BẢN SAO từ `Rheinmir/setup` (`fdk/tools/`), giấy phép SIL OFL ở `engine/LexendDeca-NOTICE.txt`.
- Khai `repo_role: module` trong `.overstack.yaml` cho `/ship` của overstack.

### Fixed
- Gỡ 3 trang control-room bị commit nhầm từ 3.0.0 (do test sinh ra khi chưa có `ORCA_GRAPH_NO_ROOM`).

## 3.0.2 — 2026-09-20

### Fixed
- Luồng gõ tay `lock` → `set dispatched` nay cũng đăng ký store vào registry và bật daemon `watch` như `run`. Trước đây chỉ `run` làm việc đó, nên dispatch thủ công không có ai canh lease và control-room đứng im sau lần đổi state cuối.

## 3.0.1 — 2026-09-20

### Fixed
- Store tự ghi `.gitignore` (`.admission.lock*`, `*.locks/`) ở lần `lock`/`add-node` đầu tiên nếu chưa có — file khoá runtime không còn làm bẩn cây git của dự án chứa store. Store đã có `.gitignore` riêng thì không bị đụng.

## 3.0.0 — 2026-09-20

Tách khỏi `Rheinmir/setup` (commit gốc `70ecb22`) thành repo riêng. Áp phần nằm trong tầm tool file-based của Reprise Graph Engine PRD v1.1 (§22–30).

### Added
- Lý do cạnh `**Depends:** Task N (reason)` và lệnh `audit-edges` (chỉ đọc): `EDGE_UNJUSTIFIED`, `EDGE_INFERRED`, `PREFERENCE_REMOVABLE`, `PREFERENCE_HAS_HIDDEN_CONSTRAINT`, critical path đếm theo node trước/sau.
- `**Resources:**` với mode exclusive, shared, capacity:N; admission lúc `lock` dưới một mutex toàn store, soi claim xuyên graph.
- `ask <id> waiting` và dòng "ready nhưng chưa lock được" trong `next`.
- `add-node` (kèm `--blocks` để chèn vào giữa); build lại graph con giữ liên kết mẹ.
- `reconcile-items`, `dry-streak`, `cost-envelope`; hai hàm thuần `reconcile_verdicts`, `next_dry_streak` chép nguyên văn PRD.
- graph-viz: cạnh preference chấm thưa, cổng control/acceptance màu hổ phách, thẻ node hiện reason và claim.
- `evals/` — ma trận VT 28 kịch bản + scoreboard + history; `install.sh`; CI.

### Fixed (review độc lập trước khi phát hành — mỗi mục có test `test_review_*`)
- Parser từng nuốt im lặng token `**Depends:**` không hiểu (ghi chú chen vào, `và`, task không tồn tại) → cạnh biến mất, node ready sớm. Nay là lỗi build.
- `add-node` nhìn PLAN bằng quét thô: dòng mẫu trong code fence bị coi là thật (sửa nhầm `**Depends:**` trong fence, cấp sai id, chèn sau `## Origin`). Nay dùng đúng con mắt của parser và kiểm lại cạnh `--blocks` trước khi ghi.
- Hai `add-node` song song đè mất task của nhau → bọc trong mutex admission. Field chứa xuống dòng (chèn được `### Task` giả) → từ chối.
- `run`: replan giữa lúc agent chạy vẫn publish kết quả của plan cũ → nay STALE, node về `ready`.
- Claim xuyên graph kẹt vĩnh viễn khi lease hết ở graph khác hoặc khi `unlock` tay → `lock`/`next` chạy reaper cho cả store; `unlock` node đang chạy đưa state về `unknown`.
- Mutex admission O_EXCL + mtime có TOCTOU khi phá khoá stale (đo 1/150 vòng) → chuyển sang `flock`.
- `capacity:N` phụ thuộc thứ tự lock và `shared` lách được quota → N là kích thước pool, lấy min, mọi holder chiếm slot; `capacity:0` bị từ chối.
- `audit-edges` đề xuất bỏ cạnh preference dù downstream dùng output của upstream trong Consumes/Verify → giữ.
- `reconcile-items` để lọt dòng lỗi mâu thuẫn / ID lạ, và coi file verdict thiếu hay manifest rỗng là "đủ" → lỗi giao thức.
- graph-viz chưa escape dep id và kind; parser nay cũng chặn hai field này theo regex.

### Changed
- `kind-glyphs.local.json` chuyển về `~/.orca-graph/` (trước nằm cạnh sổ mặc định trong thư mục skill) để không làm bẩn git clone của engine.
- Store mặc định nhận thêm biến `ORCA_GRAPH_DIR`.

### Known limitations
- 13/28 kịch bản VT ngoài phạm vi, lý do ghi trong `evals/vt-matrix.json`.
- Mutex admission là một `flock` cho cả store; đủ cho vài agent, chưa đo dưới tải lock dồn dập. Python Windows thuần (không có `fcntl`) rơi về O_EXCL, crash giữa chừng phải xoá tay file `.admission.lock.excl`.
- Hidden-constraint của `audit-edges` là so chuỗi (file, output trong backtick); coupling không lộ ra chữ thì không bắt được — vì vậy nó chỉ ĐỀ XUẤT.
- `audit-edges` ước lượng critical path theo số node, không có thời lượng.
