#!/usr/bin/env bash
# install-test — cài từ CHÍNH checkout này (file:// git) vào thư mục tạm, kiểm: clone, launcher, --check, update idempotent, cây bẩn không bị reset đè.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
SRC="$T/src.git"; git clone -q --bare "$ROOT" "$SRC"
BR=itest; git -C "$SRC" branch -f "$BR" HEAD >/dev/null      # CI checkout là detached HEAD → tự đặt nhánh để clone -b được
export ORCA_GRAPH_REPO="$SRC" ORCA_GRAPH_REF="$BR" ORCA_GRAPH_INSTALL_DIR="$T/home/repo" HOME="$T/fakehome"; mkdir -p "$HOME/.claude"
ok(){ echo "  ok  $*"; }; die(){ echo "  FAIL $*" >&2; exit 1; }
bash "$ROOT/install.sh" --check >/dev/null 2>&1 && die "--check phải rc≠0 khi chưa cài"; ok "--check báo chưa cài"
bash "$ROOT/install.sh" >/dev/null; [ -f "$T/home/repo/engine/orca-graph.py" ] || die "thiếu engine"; ok "clone + engine"
"$T/home/bin/orca-graph" --version | grep -q "orca-graph $(cat "$ROOT/VERSION")" || die "launcher/version lệch VERSION"; ok "launcher khớp VERSION"
[ -f "$HOME/.claude/skills/orca-graph/SKILL.md" ] || die "skill chưa copy"; ok "skill copy"
bash "$ROOT/install.sh" >/dev/null; ok "chạy lại idempotent"
echo "# sửa tay" >> "$T/home/repo/engine/orca-graph.py"
OUT="$(bash "$ROOT/install.sh")"; grep -q "bỏ qua cập nhật" <<<"$OUT" || die "cây bẩn phải được giữ nguyên"   # không pipe thẳng vào grep -q: SIGPIPE + pipefail
tail -1 "$T/home/repo/engine/orca-graph.py" | grep -q "sửa tay" || die "install đè mất sửa tay"; ok "cây bẩn không bị reset"
echo "install-test: PASS"
