#!/usr/bin/env bash
# SessionStart hook: orient the agent in a few lines - what this repo is, the
# workflow rule that bites most often, and the current branch. Output is added
# to the session context.
set -uo pipefail

echo "💳 spin-payments - Django payments gateway (multi-provider) + Celery. Backend only."
echo "   Brief: AGENTS.md · run: 'make help' · gate: 'make check' (ruff + tests)"
echo "   ⚠️  Workflow: 'main' (prod) and 'develop' (stage) are PROTECTED - branch + reviewed PR only."

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -n "${branch:-}" ]; then
  case "$branch" in
    main|develop)
      echo "   ⚠️  You are on '$branch' (a protected/deploy branch). Create a feature branch before committing:"
      echo "        git switch -c feat/<topic>"
      ;;
    *)
      echo "   On branch '$branch'."
      ;;
  esac
fi

exit 0
