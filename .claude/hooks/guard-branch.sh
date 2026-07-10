#!/usr/bin/env bash
# PreToolUse(Bash) guard: never let an agent commit on — or push to — a
# PROTECTED branch. Here that is `main` (auto-deploys prod) and `develop`
# (auto-deploys stage). See .github/workflows/{prod,stage}.yml.
#
# Enforces AGENTS.md -> Workflow: "develop and main are protected. Every change
# lands on a feature branch and merges via a reviewed PR."
#
# Best-effort: a convenience guardrail, not a security control. The real
# protection is branch protection on the remote. Exit 2 blocks the tool call and
# feeds the message back to the agent.
#
# Worktree-aware. The branch is resolved in the *tree the guarded git command
# acts on*, NOT the hook's own cwd: we parse a leading `cd <dir>` and/or a
# `git -C <dir>` out of each command segment and run
#   git -C <target> rev-parse --abbrev-ref HEAD
# there. A commit on a feature-branch worktree is allowed even while the main
# checkout sits on `main`/`develop`.
#
# Precision: the command string may merely *mention* "git commit" / "git push
# origin main" inside an argument or a heredoc body. We strip heredoc bodies and
# quoted spans (multiline-aware), then inspect only the first command token of
# each &&/||/;/|/&-split segment, so argument/body text can't masquerade as a
# real invocation.
set -uo pipefail
set -f   # no glob: the `set -- $seg` word-splits below must not expand filenames

# Protected refs (space-separated). Add a branch here to guard it too.
PROTECTED="main develop"

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

# Directory the guarded command runs in by default: the hook-provided session
# cwd, else CLAUDE_PROJECT_DIR, else our own pwd. A `cd`/`-C` in the command
# overrides this per segment (below).
base_dir="${hook_cwd:-}"
[ -z "$base_dir" ] && base_dir="${CLAUDE_PROJECT_DIR:-}"
[ -z "$base_dir" ] && base_dir="$PWD"

is_protected() {  # $1: branch name
  local b
  for b in $PROTECTED; do
    [ "$1" = "$b" ] && return 0
  done
  return 1
}

# Build a "skeleton" of the command with heredoc bodies and quoted spans
# removed, so only real command tokens survive. Two awk passes, portable.
skeleton=$(printf '%s' "$cmd" | awk '
  BEGIN { sq=sprintf("%c",39); dq=sprintf("%c",34); delim=""; strip=0 }
  {
    if (delim != "") {
      t=$0
      if (strip) sub(/^\t+/, "", t)
      if (t == delim) { delim=""; next }
      next
    }
    line=$0
    if (match(line, /<<-?[ \t]*/)) {
      op=substr(line, RSTART, RLENGTH)
      rest=substr(line, RSTART+RLENGTH)
      strip=(op ~ /-/) ? 1 : 0
      first=substr(rest,1,1)
      if (first==sq || first==dq) rest=substr(rest,2)
      if (match(rest, /^[A-Za-z_][A-Za-z0-9_]*/))
        delim=substr(rest, RSTART, RLENGTH)
      else
        delim=""
    }
    print line
  }
' | awk '
  BEGIN { sq=sprintf("%c",39); dq=sprintf("%c",34) }
  { buf = buf $0 "\n" }
  END {
    n=length(buf); s=0; d=0; out=""
    for (i=1;i<=n;i++) {
      c=substr(buf,i,1)
      if (s) { if (c==sq) s=0; continue }
      if (d) { if (c==dq) d=0; continue }
      if (c==sq) { s=1; continue }
      if (c==dq) { d=1; continue }
      if (c=="\\") { i++; out=out substr(buf,i,1); continue }
      out=out c
    }
    printf "%s", out
  }
')

# Split the skeleton into ordered segments on the shell sequencing operators, so
# `cd <dir> && git …` is seen as two segments in order.
segments=$(printf '%s' "$skeleton" \
  | sed -E 's/&&/\n/g; s/\|\|/\n/g; s/;/\n/g; s/\|/\n/g; s/&/\n/g')

block() {
  {
    echo "⛔ $1"
    echo "   AGENTS.md: 'main' (prod) and 'develop' (stage) are PROTECTED - branch + reviewed PR only."
    echo "   Start a branch first, e.g.:  git switch -c <type>/<topic>   (feat/…, fix/…, chore/…)"
    echo "   Then commit there and open a PR with 'gh pr create'. Agents never merge their own PRs."
  } >&2
  exit 2
}

current_dir="$base_dir"

resolve_dir() {  # $1: dir token, resolved against the running $current_dir
  case "$1" in
    /*|"~"*) printf '%s' "$1" ;;
    *)       printf '%s/%s' "$current_dir" "$1" ;;
  esac
}

branch_at() { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || true; }

# Whole-ref alternation for the explicit-push check: main|develop
ref_alt=$(printf '%s' "$PROTECTED" | tr ' ' '|')

while IFS= read -r seg; do
  seg="${seg#"${seg%%[![:space:]]*}"}"      # ltrim
  [ -z "$seg" ] && continue
  # shellcheck disable=SC2086
  set -- $seg
  [ $# -eq 0 ] && continue

  # Skip leading `VAR=value` environment assignments.
  while [ $# -gt 0 ]; do
    case "$1" in
      [A-Za-z_]*=*) shift ;;
      *) break ;;
    esac
  done
  [ $# -eq 0 ] && continue

  if [ "$1" = "cd" ]; then
    [ -n "${2:-}" ] && [ "$2" != "-" ] && current_dir="$(resolve_dir "$2")"
    continue
  fi

  [ "$1" = "git" ] || continue
  shift

  # Consume git's global options to find both the subcommand and target tree.
  target="$current_dir"
  while [ $# -gt 0 ]; do
    case "$1" in
      -C)       shift; [ $# -gt 0 ] && { target="$(resolve_dir "$1")"; shift; } ;;
      -c)       shift; [ $# -gt 0 ] && shift ;;
      --git-dir|--work-tree|--namespace|--exec-path)
                shift; [ $# -gt 0 ] && shift ;;
      --*=*)    shift ;;
      -*)       shift ;;
      *)        break ;;
    esac
  done
  subcmd="${1:-}"
  [ $# -gt 0 ] && shift
  rest="$*"

  case "$subcmd" in
    commit)
      cur="$(branch_at "$target")"
      is_protected "$cur" && block "Refusing to commit directly on '$cur'."
      ;;
    push)
      # Explicit protected destination - `origin main`, `HEAD:develop`,
      # `refs/heads/main`, `+develop` - matched as a whole ref token so
      # `main-fix` / `develop-x` / `main/foo` stay safe.
      if printf '%s' " $rest " | grep -Eq "(:|[[:space:]])[+]?(refs/heads/)?(${ref_alt})([^A-Za-z0-9._/-]|$)"; then
        block "Refusing to push to a protected branch."
      fi
      # Bare push (no refspec) while the target tree is itself on a protected branch.
      bare=1
      for t in $rest; do
        case "$t" in
          -*|origin|upstream) ;;
          *) bare=0; break ;;
        esac
      done
      if [ "$bare" = 1 ]; then
        cur="$(branch_at "$target")"
        is_protected "$cur" && block "Refusing to push to '$cur'."
      fi
      ;;
  esac
done <<< "$segments"

exit 0
