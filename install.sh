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

raw_install(){ # không git (hoặc bản cài cũ kiểu raw): kéo đúng 4 file engine — chạy được, nhưng không có `git log`, tests/, evals/
  case "$REPO" in https://github.com/*) ;; *) echo "không có git và ORCA_GRAPH_REPO=$REPO không phải https://github.com/… — không dựng được URL raw; cài git rồi chạy lại" >&2; exit 1;; esac
  local RAW="${REPO%.git}"; RAW="https://raw.githubusercontent.com/${RAW#https://github.com/}/$REF"
  say "tải file engine từ $RAW (không có git)"
  mkdir -p "$DEST/engine"
  for f in orca-graph.py graph-viz.py graph-atlas.py kind-glyphs.json; do curl -fsSL "$RAW/engine/$f" -o "$DEST/engine/$f"; done
}

if [ -L "$DEST" ]; then
  # Symlink = ai đó trỏ thư mục cài sang BẢN DEV của họ. Installer không bao giờ fetch/reset/merge vào đó.
  say "⚠ $DEST là symlink → $(readlink "$DEST") (bản dev) — KHÔNG cập nhật; đang dùng: $(ver)"
elif [ -d "$DEST/.git" ]; then
  # Ba thứ KHÔNG được đè: cây bẩn · commit local chưa có ở remote · lịch sử đầy đủ (không ép thành shallow).
  if [ -n "$(git -C "$DEST" status --porcelain 2>/dev/null)" ]; then
    say "⚠ $DEST có thay đổi chưa commit — bỏ qua cập nhật (commit/stash rồi chạy lại); đang dùng: $(ver)"
  elif [ "$(git -C "$DEST" rev-parse --is-shallow-repository 2>/dev/null)" = true ]; then
    # clone --depth 1 do chính installer tạo: đúng 1 commit. Nhiều hơn = có người commit thêm tại chỗ → không reset.
    if [ "$(git -C "$DEST" rev-list --count HEAD)" -gt 1 ]; then
      say "⚠ $DEST có commit local trên bản shallow — bỏ qua cập nhật; đang dùng: $(ver)"
    elif ! git -C "$DEST" fetch -q --depth 1 origin "$REF"; then
      say "⚠ fetch lỗi (mạng / ref '$REF' không tồn tại?) — giữ bản đang có: $(ver)"
    else
      say "cập nhật $DEST → $REF"
      git -C "$DEST" reset -q --hard FETCH_HEAD || say "⚠ reset lỗi — giữ bản đang có: $(ver)"
    fi
  elif ! git -C "$DEST" fetch -q origin "$REF"; then
    say "⚠ fetch lỗi (mạng / ref '$REF' không tồn tại?) — giữ bản đang có: $(ver)"
  elif [ -n "$(git -C "$DEST" rev-list FETCH_HEAD..HEAD)" ]; then
    say "⚠ $DEST có commit CHƯA push (đi trước $REF) — bỏ qua cập nhật để không mất việc; đang dùng: $(ver)"
  else
    say "cập nhật $DEST → $REF (fast-forward)"
    git -C "$DEST" merge -q --ff-only FETCH_HEAD || say "⚠ không fast-forward được — giữ bản đang có: $(ver)"
  fi
elif [ -f "$DEST/engine/orca-graph.py" ]; then
  raw_install                                   # bản cài kiểu raw từ trước → cập nhật cùng kiểu, kể cả khi máy giờ đã có git
elif command -v git >/dev/null; then
  [ -e "$DEST" ] && { echo "$DEST tồn tại nhưng không phải bản cài orca-graph — dời đi rồi chạy lại" >&2; exit 1; }
  mkdir -p "$(dirname "$DEST")"
  say "clone $REPO ($REF) → $DEST"
  git clone -q --depth 1 -b "$REF" "$REPO" "$DEST"
else
  raw_install
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
say "  chạy thẳng: $BIN/orca-graph --help$([ -f "$DEST/evals/run.py" ] && echo "   ·   eval: python3 $DEST/evals/run.py --check")"
[ "$DEST" = "$HOME/.orca-graph/repo" ] || say "  thư mục cài tuỳ biến → shim của overstack cần: export ORCA_GRAPH_INSTALL_DIR=$DEST"
