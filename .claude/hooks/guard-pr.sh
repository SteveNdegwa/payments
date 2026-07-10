#!/usr/bin/env bash
# PreToolUse(Bash) guard for `gh pr create` / `gh pr edit`:
#
#   NO AI-attribution footer. PR titles/bodies must not carry a "Generated with
#   Claude Code" line or a claude.ai/code session link (or any equivalent AI
#   trailer). PR bodies follow .github/PULL_REQUEST_TEMPLATE.md. Commit co-author
#   trailers are NOT touched - AGENTS.md requires those; this guard is PR-only.
#
# Best-effort convenience guard, not a security control - the real backstop is
# review. It scans the inline command AND any --body-file the command points at.
# Exit 2 blocks the call and feeds the message back to the agent.
set -uo pipefail

[ -t 0 ] && exit 0
input=$(cat)

if command -v jq >/dev/null 2>&1; then
  cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
  hook_cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
else
  cmd=$(printf '%s' "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\(.*\)".*/\1/p' | head -1)
  hook_cwd=""
fi
[ -z "${cmd:-}" ] && exit 0

# Only police PR creation/editing.
printf '%s' "$cmd" | grep -Eq 'gh[[:space:]]+pr[[:space:]]+(create|edit)([[:space:]]|$)' || exit 0

# Working tree the PR is created from (for resolving a relative --body-file).
workdir="${hook_cwd:-}"
[ -z "$workdir" ] && workdir="${CLAUDE_PROJECT_DIR:-}"
[ -z "$workdir" ] && workdir="$PWD"
first_cd=$(printf '%s' "$cmd" | head -1 \
  | sed -nE 's/^[[:space:]]*cd[[:space:]]+([^[:space:]&;|]+).*/\1/p' \
  | sed "s/[\"']//g")
if [ -n "$first_cd" ]; then
  case "$first_cd" in
    /*|"~"*) workdir="$first_cd" ;;
    *)       workdir="$workdir/$first_cd" ;;
  esac
fi

# Body content = the raw command (covers --title and inline -b/--body text) plus
# the contents of any --body-file / -F the command references.
content=$cmd
paths=$(printf '%s' "$cmd" \
  | grep -oE -- '(--body-file|-F)([[:space:]]+|=)[^[:space:]]+' \
  | sed -E 's/^(--body-file|-F)([[:space:]]+|=)//' \
  | sed "s/[\"']//g")
while IFS= read -r p; do
  [ -z "$p" ] && continue
  [ -e "$p" ] || { [ -e "$workdir/$p" ] && p="$workdir/$p"; }
  [ -e "$p" ] || { [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -e "$CLAUDE_PROJECT_DIR/$p" ] && p="$CLAUDE_PROJECT_DIR/$p"; }
  [ -f "$p" ] && content="$content
$(cat "$p")"
done <<EOF
$paths
EOF

if printf '%s' "$content" | grep -Eiq 'claude\.ai/code|claude\.com/claude-code|generated with[[:space:]]+\[?claude code'; then
  { printf '%s\n' \
    "⛔ Refusing to open/edit a PR with an AI-attribution footer." \
    "   AGENTS.md: PR titles/bodies carry NO 'Generated with Claude Code' line or" \
    "   claude.ai/code session link. Remove it. PR bodies follow" \
    "   .github/PULL_REQUEST_TEMPLATE.md (Context / Changes Made / Testing)." \
    "   (Commit co-author trailers are fine - this only blocks the PR footer.)"
  } >&2
  exit 2
fi

exit 0
