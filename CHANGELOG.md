# Changelog

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

### Changed
- `kind-glyphs.local.json` chuyển về `~/.orca-graph/` (trước nằm cạnh sổ mặc định trong thư mục skill) để không làm bẩn git clone của engine.
- Store mặc định nhận thêm biến `ORCA_GRAPH_DIR`.

### Known limitations
- 13/28 kịch bản VT ngoài phạm vi, lý do ghi trong `evals/vt-matrix.json`.
- Mutex admission là một khoá cho cả store; đủ cho vài agent, chưa đo dưới tải lock dồn dập.
- `audit-edges` ước lượng critical path theo số node, không có thời lượng.
