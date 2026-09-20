#!/usr/bin/env bash
# install.sh — cài / cập nhật engine orca-graph vào máy (KHÔNG vào từng dự án).
#
#   curl -fsSL https://raw.githubusercontent.com/Rheinmir/orca-graph/main/install.sh | bash
#
# Kết quả:
#   ~/.orca-graph/repo/          git clone của repo này (theo dõi được: `git -C ~/.orca-graph/repo log`)
#   ~/.orca-graph/bin/orca-graph launcher chạy thẳng (không cần overstack)
#   ~/.claude/skills/orca-graph  skill /orca-graph (chỉ khi máy có ~/.claude; bỏ bằng --no-skill)
# overstack (Rheinmir/setup) gọi engine qua shim: harness/scripts/orca-graph.py → ~/.orca-graph/repo/engine/.
#
# Biến:  ORCA_GRAPH_REF (nhánh/tag, mặc định main) · ORCA_GRAPH_REPO (URL git) · ORCA_GRAPH_INSTALL_DIR (mặc định ~/.orca-graph/repo)
# Cờ:    --no-skill · --check (chỉ báo trạng thái, không đổi gì)
set -euo pipefail
REPO="${ORCA_GRAPH_REPO:-https://github.com/Rheinmir/orca-graph.git}"
REF="${ORCA_GRAPH_REF:-main}"
DEST="${ORCA_GRAPH_INSTALL_DIR:-$HOME/.orca-graph/repo}"
WITH_SKILL=1; CHECK=0
for a in "$@"; do case "$a" in --no-skill) WITH_SKILL=0;; --check) CHECK=1;; *) echo "cờ lạ: $a" >&2; exit 2;; esac; done
say(){ printf '\033[1;35m[orca-graph]\033[0m %s\n' "$*"; }
ver(){ python3 "$DEST/engine/orca-graph.py" --version 2>/dev/null || echo "orca-graph ?"; }

if [ "$CHECK" = 1 ]; then
  if [ -f "$DEST/engine/orca-graph.py" ]; then say "đã cài: $(ver) @ $(git -C "$DEST" rev-parse --short HEAD 2>/dev/null || echo no-git) — $DEST"; exit 0; fi
  say "chưa cài ($DEST)"; exit 1
fi
command -v python3 >/dev/null || { echo "cần python3" >&2; exit 1; }

if [ -d "$DEST/.git" ]; then
  # Cây bẩn = có người đang sửa engine tại chỗ (hoặc DEST là symlink tới bản dev) → KHÔNG reset đè, tránh mất việc.
  if [ -n "$(git -C "$DEST" status --porcelain 2>/dev/null)" ]; then
    say "⚠ $DEST có thay đổi chưa commit — bỏ qua cập nhật (commit/stash rồi chạy lại)"; say "đang dùng: $(ver)"
  else
    say "cập nhật $DEST → $REF"
    git -C "$DEST" fetch -q --depth 1 origin "$REF" && git -C "$DEST" reset -q --hard FETCH_HEAD \
      || say "⚠ fetch lỗi (mạng?) — giữ bản đang có: $(ver)"
  fi
elif command -v git >/dev/null; then
  [ -e "$DEST" ] && { echo "$DEST tồn tại nhưng không phải git clone — dời đi rồi chạy lại" >&2; exit 1; }
  mkdir -p "$(dirname "$DEST")"
  say "clone $REPO ($REF) → $DEST"
  git clone -q --depth 1 -b "$REF" "$REPO" "$DEST"
else
  # không có git: kéo đúng 4 file engine qua raw (mất khả năng `git log`, vẫn chạy được)
  RAW="${REPO%.git}"; RAW="https://raw.githubusercontent.com/${RAW#https://github.com/}/$REF"
  say "không có git — tải file engine từ $RAW"
  mkdir -p "$DEST/engine"
  for f in orca-graph.py graph-viz.py graph-atlas.py kind-glyphs.json; do curl -fsSL "$RAW/engine/$f" -o "$DEST/engine/$f"; done
fi
[ -f "$DEST/engine/orca-graph.py" ] || { echo "cài lỗi: thiếu $DEST/engine/orca-graph.py" >&2; exit 1; }

BIN="$(dirname "$DEST")/bin"; mkdir -p "$BIN"
printf '#!/usr/bin/env bash\nexec python3 "%s/engine/orca-graph.py" "$@"\n' "$DEST" > "$BIN/orca-graph"; chmod +x "$BIN/orca-graph"

if [ "$WITH_SKILL" = 1 ] && [ -d "$HOME/.claude" ] && [ -d "$DEST/skills/orca-graph" ]; then
  mkdir -p "$HOME/.claude/skills/orca-graph"
  cp -R "$DEST/skills/orca-graph/." "$HOME/.claude/skills/orca-graph/"
  say "✓ skill /orca-graph → ~/.claude/skills/orca-graph"
fi
say "✓ $(ver) @ $(git -C "$DEST" rev-parse --short HEAD 2>/dev/null || echo raw) — $DEST"
say "  chạy thẳng: $BIN/orca-graph --help   ·   eval: python3 $DEST/evals/run.py --check"
