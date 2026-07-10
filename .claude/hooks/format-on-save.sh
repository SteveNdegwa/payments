#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit): auto-format the Python file just written, so
# agent edits land already ruff-clean. Best-effort and SILENT - it never blocks
# an edit and always exits 0. The real gate is `make lint` / CI; this just
# removes the busywork of re-formatting after every write.
#
# Runs `ruff format` + `ruff check --fix --select I` (import sorting) via uv when
# available (respects pyproject.toml: line length 100, py314). Skips generated
# migrations, which ruff is configured to ignore anyway.
set -uo pipefail

payload="$(cat)"
file="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$file" ] || exit 0
[ -f "$file" ] || exit 0

case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac

# Never touch generated migrations (ruff excludes them; keep them byte-stable).
case "$file" in
  */migrations/*) exit 0 ;;
esac

root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)}"

run_ruff() {  # $@: ruff args
  if command -v uv >/dev/null 2>&1; then
    ( cd "$root" && uv run ruff "$@" ) >/dev/null 2>&1
  elif command -v ruff >/dev/null 2>&1; then
    ( cd "$root" && ruff "$@" ) >/dev/null 2>&1
  fi
}

run_ruff check --fix --select I "$file"   # isort-style import ordering
run_ruff format "$file"
exit 0
