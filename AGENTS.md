# AGENTS.md — phạm vi của repo này (đọc TRƯỚC khi sửa)

## Thuộc repo NÀY (Rheinmir/orca-graph) — sửa ở đây
| Thứ | Ở đâu |
|---|---|
| Logic engine: parser PLAN, state machine, lock/lease/claim, audit-edges, add-node, reconcile-items… | `engine/orca-graph.py` |
| Vẽ 1 graph / atlas, sổ icon theo Kind | `engine/graph-viz.py` · `engine/graph-atlas.py` · `engine/kind-glyphs.json` |
| Font mặc định của trang sinh ra (Be Vietnam Pro 400/600/800, nhúng) — **BẢN SAO**, nguồn ở framework `fdk/tools/html_font.py`; đổi ở đó rồi chép đè | `engine/html_font.py` · `engine/html_font_data.py` · `engine/html_base.py` (lớp nền: token sáng/tối, toggle, quy ước viết hoa) |
| Skill `/orca-graph` (bản CANONICAL) | `skills/orca-graph/SKILL.md` |
| Test engine, test cài đặt | `tests/` |
| Ma trận nghiệm thu VT-01…28 (PRD Reprise v1.1 §30.1) + scoreboard + history | `evals/` |
| Trình cài engine vào `~/.orca-graph/repo` | `install.sh` |

## KHÔNG thuộc repo này — ở overstack (Rheinmir/setup, nhánh `orca`)
- 3 **shim** giữ đường dẫn cũ: `harness/scripts/orca-graph.py`, `fdk/tools/graph-viz.py`, `fdk/tools/graph-atlas.py`. Chỉ tìm engine rồi `exec`; KHÔNG chứa logic. Sửa logic vào shim là sai chỗ.
- Cockpit `fdk/tools/build-control-room.py` (cần `overstack_paths`), hook nhắc goal→graph, skill `/tc-run`.
- Option cài "orca-graph đã tick sẵn": `harness/poc-vendor-neutral/install.sh` + test `harness/tests/install-graph-option-test.sh`, `test_control_room.py`.
- Bản MIRROR của SKILL.md + provenance ghim commit: `skills/orca-graph/SKILL.md`, `fdk/skills.provenance.json`.
- Store dữ liệu graph của từng dự án (`llmwiki/graph/` hoặc `.llmwiki/graph/`) — là DATA của dự án, không phải của engine.

## Ràng buộc khi sửa engine
- File-based, một máy, chỉ stdlib Python ≥ 3.9. Cần DB/outbox/scheduler = engine khác (xem "Ngoài phạm vi" trong README).
- `events.jsonl` append-only là sự thật; `graph.json` là cache. `schema_version` giữ 1, field mới phải tuỳ chọn (graph cũ không được gãy).
- Engine chạy DƯỚI shim: `__file__` có thể là đường shim, thư mục engine thật là `globals().get("__engine_dir__")`. Đừng ghi file vào thư mục engine (đó là git clone của người dùng) — state máy-local để ở `~/.orca-graph/`.
- Mỗi hành vi mới: 1 test pytest; nếu ứng với một VT thì tên test chứa `VTnn` và cập nhật `evals/vt-matrix.json`. Không nhận vơ: chưa đủ thì `partial`/`out_of_scope` kèm lý do.

## Quy trình sau khi sửa
Repo này khai `repo_role: module` trong `.overstack.yaml` — `/ship` của overstack đọc nhãn đó và đi đúng luồng dưới đây (không chạy `ci-local`/`capability-stamp`, là việc của repo framework).
1. `python3 -m pytest -q tests && python3 evals/run.py && bash tests/install-test.sh` — **commit trước** khi chạy install-test (nó clone từ HEAD).
2. Bump `VERSION` + hằng `VERSION` trong engine + `CHANGELOG.md` → push → tag `vX.Y.Z`.
3. Bên overstack, chỉ khi cần: SKILL đổi → copy sang `skills/orca-graph/SKILL.md` → `python3 harness/scripts/sync-skills.py`; ghim lại `commit`/`version` trong `fdk/skills.provenance.json`; máy khách cần nhận thay đổi shim/installer → `python3 harness/scripts/capability-stamp.py --update`.
