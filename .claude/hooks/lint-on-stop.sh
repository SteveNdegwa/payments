#!/usr/bin/env bash
# Stop hook: advisory lint pass over the Python files changed this session.
# Surfaces ruff lint + format drift so it's caught before review - but it is
# ADVISORY ONLY: it prints findings and always exits 0, never trapping the
# session in a loop. The real gate is `make check` + CI. macOS bash 3.2-safe
# (no mapfile / associative arrays).
set -uo pipefail

root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$root" || exit 0

# Staged + unstaged + untracked paths (strip the 3-char porcelain status prefix).
changed="$(git status --porcelain 2>/dev/null | sed 's/^...//')"
[ -n "$changed" ] || exit 0

# Python files, excluding generated migrations and the virtualenv.
py_files="$(printf '%s\n' "$changed" | grep -E '\.py$' | grep -vE '/migrations/|^\.venv/' || true)"
[ -n "$py_files" ] || exit 0

# Runner: prefer uv, fall back to a bare ruff on PATH.
ruff() {
  if command -v uv >/dev/null 2>&1; then uv run ruff "$@"
  elif command -v command >/dev/null 2>&1 && command -v ruff >/dev/null 2>&1; then command ruff "$@"
  else return 127; fi
}
command -v uv >/dev/null 2>&1 || command -v ruff >/dev/null 2>&1 || exit 0

issues=0

# shellcheck disable=SC2086
lint_out="$(printf '%s\n' $py_files | xargs ruff check 2>&1 || true)"
if printf '%s' "$lint_out" | grep -qiE 'error|warning|[0-9]+ (error|warning)'; then
  printf '⚠️  ruff check:\n%s\n' "$lint_out"
  issues=1
fi

# shellcheck disable=SC2086
fmt_out="$(printf '%s\n' $py_files | xargs ruff format --check 2>&1 || true)"
if printf '%s' "$fmt_out" | grep -qiE 'would reformat'; then
  printf '⚠️  ruff format - these files would be reformatted:\n%s\n' "$fmt_out"
  issues=1
fi

[ "$issues" -eq 1 ] && printf '   (advisory only - full gate: make check)\n'
exit 0
