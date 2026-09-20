# orca-graph

Engine phân việc dạng **đồ thị phụ thuộc** cho agent: đọc một `PLAN.md`, dựng graph có deps là dữ liệu, dispatch theo khoá + lease + generation, giữ state bền bằng sổ append-only, và vẽ graph ra HTML. File-based, một máy, chỉ cần Python 3.9+ chuẩn — không DB, không service.

Repo này tách ra từ [overstack](https://github.com/Rheinmir/setup) (commit gốc `70ecb22`, ngày 20/09/2026) để engine có lịch sử, test và bộ eval riêng. overstack vẫn gọi nó qua shim ở đúng đường dẫn cũ (`harness/scripts/orca-graph.py`, `fdk/tools/graph-viz.py`, `fdk/tools/graph-atlas.py`).

## Cài

```bash
curl -fsSL https://raw.githubusercontent.com/Rheinmir/orca-graph/main/install.sh | bash
```

Lệnh này clone repo vào `~/.orca-graph/repo/`, tạo launcher `~/.orca-graph/bin/orca-graph`, và nếu máy có `~/.claude` thì copy skill `/orca-graph` vào `~/.claude/skills/`. Chạy lại là cập nhật. Nếu thư mục cài có thay đổi chưa commit, script **không** reset đè mà chỉ cảnh báo. Trình cài của overstack hiện option này đã tick sẵn, nên người dùng overstack thường không cần chạy tay.

Biến môi trường: `ORCA_GRAPH_REF` (nhánh hoặc tag, mặc định `main`), `ORCA_GRAPH_REPO`, `ORCA_GRAPH_INSTALL_DIR`. Cờ: `--no-skill`, `--check`.

## Dùng nhanh

```bash
og=~/.orca-graph/bin/orca-graph
$og build docs/my-PLAN.md            # PLAN → graph (cycle, xung đột ghi file, claim tài nguyên)
$og audit-edges my                   # cạnh thiếu lý do · cạnh preference bỏ được · critical path trước/sau (chỉ đọc)
$og ask my waiting                   # mỗi node đang chờ vì data | gate | resource | queue | control | retry
$og next my                          # node chạy song song được ngay
$og run my t1 -- <lệnh agent>        # lock → dispatch → heartbeat → verify → done/failed
$og add-node my --title "Viết changelog" --depends t3:data --files CHANGELOG.md --verify "test -s CHANGELOG.md"
python3 ~/.orca-graph/repo/engine/graph-viz.py <store>/my.graph.json   # HTML một graph
```

Một task trong PLAN:

```markdown
### Task 4: migrate bảng booking
**Kind:** migrate
**Depends:** Task 2 (contract), Task 3 (preference)
**Resources:** db-migration(exclusive), staging-api(capacity:2)
**Files:**
- Sửa: `db/migrations/004_booking.sql`
**Verify:** `make test-migrations`
```

Hướng dẫn đầy đủ cho agent nằm ở [`skills/orca-graph/SKILL.md`](skills/orca-graph/SKILL.md).

## Bản 3.0 có gì (theo Reprise Graph Engine PRD v1.1)

| Năng lực | Lệnh / cú pháp | Mục PRD |
|---|---|---|
| Cạnh có lý do, audit topology chỉ đọc | `**Depends:** Task 1 (data)` · `audit-edges` | §23.2–23.3 |
| Resource claims — tranh chấp tài nguyên không phải cạnh DAG | `**Resources:** key(exclusive\|shared\|capacity:N)` · admission lúc `lock` | §23.4 |
| Lý do chờ; ready khác admitted | `ask <id> waiting` · `next` | §28.4, §8.4 |
| Thêm node theo yêu cầu, giữ lịch sử | `add-node` | §10 |
| Ghép kết quả theo định danh, coverage hai mẫu số | `reconcile-items` | §24.1, §25.5 |
| Vòng discovery khô | `dry-streak` | §26.4 |
| Tổng số lần gọi model trước dispatch | `cost-envelope` | §27.2 |

Các năng lực nền từ bản trước vẫn nguyên: graph mẹ/con tới depth 6, replan có `plan_version`, control pause/resume/cancel, QC độc lập sau verify, allow-list ghi file, daemon `watch`, sổ `answer`/`audit` chấm câu trả lời của model.

## Eval — theo dõi được qua từng bản

`evals/vt-matrix.json` ánh xạ 28 kịch bản nghiệm thu VT-01…VT-28 của PRD (§30.1) sang test thật. Kịch bản nào tool file-based không làm được thì ghi `out_of_scope` kèm lý do, không nhận vơ.

```bash
python3 -m pytest -q tests          # unit + contract
python3 evals/run.py --check        # chấm ma trận VT, rc 1 khi có VT trong phạm vi mà đỏ
python3 evals/run.py                # ghi evals/scoreboard.json + nối một dòng vào evals/history.jsonl
bash tests/install-test.sh          # cài thử từ chính checkout này
```

Trạng thái bản 3.0.0: 14 kịch bản `covered`, 1 `partial`, 13 `out_of_scope`; 15/15 kịch bản trong phạm vi pass. `history.jsonl` giữ một dòng mỗi lần chạy kèm sha256 của engine, nên so được bản nào làm rớt kịch bản nào.

## Ngoài phạm vi (nói thẳng)

Item pipeline bền có outbox và backpressure, layered fan-in theo token, anchor registry, routing model theo eval floor, scale proposal, seen ledger cho discovery, PostgreSQL ledger, sandbox process-level, secret gateway, ngân sách tiền. Những thứ này cần ledger giao dịch và scheduler thật — là một engine khác, không phải bản nâng cấp của tool này.

## Bố cục

```
engine/orca-graph.py     runtime + CLI
engine/graph-viz.py      HTML một graph        engine/graph-atlas.py   atlas 2D mọi graph
engine/kind-glyphs.json  icon/shape/màu theo **Kind:**
skills/orca-graph/       SKILL.md cho agent
tests/                   pytest + install-test.sh
evals/                   vt-matrix.json · run.py · scoreboard.json · history.jsonl
```

MIT.
