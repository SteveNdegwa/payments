# Agent configuration

This directory is the shared, **tracked** agent setup for spin-payments — it
gives every contributor and every AI tool (Claude Code, Codex, Cursor, …) the
same brief, the same guardrails, and the same project workflows. The canonical
brief lives in [`AGENTS.md`](../AGENTS.md) at the repo root; this directory holds
the machinery around it.

## Layout

```
AGENTS.md            Canonical brief — architecture, invariants, workflow.
CLAUDE.md            → symlink to AGENTS.md (so Claude reads the same file).
.agents/skills       → symlink to .claude/skills (cross-tool skill discovery).
.mcp.json            MCP servers: context7 (live Django/Celery/Stripe docs).
.editorconfig        Editor defaults (4-space Python, LF, final newline).
.codex/              Codex CLI mirror: config.toml (MCP) + hooks.json + hooks → .claude/hooks.
.claude/
  settings.json      Permission allow/deny + hook + MCP wiring (tracked, shared).
  settings.local.json  Per-machine overrides (gitignored).
  hooks/             Workflow guardrails + format/lint (see below).
  skills/            Project workflows, invoked as /<name> (see below).
  agents/            Subagent reviewers + a locator (see below).
  README.md          This file.
```

Only personal/per-session bits are gitignored (`settings.local.json`, `local/`,
`worktrees/`); everything else here is committed on purpose.

## Hooks

Registered in `settings.json`; the same scripts are reused by `.codex/hooks.json`.
All are best-effort, macOS bash-3.2-safe, and never trap the session.

- **`guard-branch.sh`** — `PreToolUse(Bash)`. Blocks `git commit` while on a
  **protected** branch (`main` = prod, `develop` = stage) and any `git push` to
  one, enforcing the AGENTS.md rule that every change lands on a feature branch
  via a reviewed PR. Worktree-aware: it resolves the branch in the *tree the git
  command acts on* (parsing a leading `cd <dir>` and/or `git -C <dir>`), so a
  feature-branch worktree commit is allowed while the main checkout is on
  `main`/`develop`. Real protection is branch protection on the remote.
- **`guard-pr.sh`** — `PreToolUse(Bash)`. Scoped to `gh pr create`/`gh pr edit`
  (scans the inline command **and** any `--body-file`). Blocks a PR title/body
  carrying an AI-attribution footer — a "Generated with Claude Code" line or a
  `claude.ai/code` session link. Commit co-author trailers are untouched.
- **`format-on-save.sh`** — `PostToolUse(Edit|Write|MultiEdit)`. Runs `ruff
  format` + import-sort on the `.py` file just written (via `uv run ruff`), so
  edits land already formatted. Skips generated migrations. Silent, never blocks.
- **`lint-on-stop.sh`** — `Stop`. Advisory `ruff check` + `ruff format --check`
  over the session's changed `.py` files. Prints findings, **always exits 0** —
  the real gate is `make check` + CI.
- **`session-start.sh`** — `SessionStart`. Prints a short orientation: what the
  repo is, the protected-branch rule, and warns if you're currently on
  `main`/`develop`.

## Skills (a.k.a. slash commands)

Each skill is a `skills/<name>/SKILL.md`. Type `/<name>` to invoke it; tools that
read skills by description (and the `.agents/skills` symlink) discover them too.
Each encodes the end-to-end recipe **with the invariants** for a common change:

- **`/add-provider`** — new `BaseProvider` subclass → registry → `Provider`/
  `ProviderAccount` data → tests (verified callbacks, idempotency, Decimal money).
- **`/add-endpoint`** — thin function view → `ResponseProvider` envelope → route
  under `/api/v1/…` → API-key auth by default → provider I/O deferred to a task.
- **`/add-model`** — `BaseModel` subclass → `makemigrations` (commit it) → admin;
  Decimal money, `TextChoices` status, idempotency indexes.
- **`/add-task`** — named, queue-routed (`payments.high|low`), **idempotent**
  Celery task with a bounded retry policy; beat schedule if periodic.

The Claude Code harness also ships generic skills (`/code-review`,
`/security-review`, `/pr`, `/run`, …).

## Subagents (`agents/`)

Delegate with e.g. "use the payment-flow-reviewer on my changes".

- **`django-backend-reviewer`** (opus) — reviews a Python diff against the
  framework conventions: thin views, `ResponseProvider`, gateway exemptions,
  migrations, task hygiene, env/secrets, query hygiene; cites `file:line`, ends
  with APPROVE / REQUEST CHANGES.
- **`payment-flow-reviewer`** (opus) — reviews money-movement changes for
  correctness: idempotency, the status/state machine, the double-entry ledger,
  callback verification, reconciliation convergence, signed webhooks, Decimal
  math. Defaults to REQUEST CHANGES when money could move incorrectly.
- **`codebase-locator`** (sonnet) — fast read-only map of where a feature lives
  across the apps (locates, doesn't review).

## MCP servers (`.mcp.json`)

`enableAllProjectMcpServers` loads them automatically. **context7** fetches
up-to-date library docs (Django, Celery, Stripe) so agents check real API shapes
instead of guessing from training data. Allow-listed as `mcp__context7__*`.

## Adding to this setup

- **A new project workflow** → add `skills/<name>/SKILL.md` (`name` +
  `description` frontmatter). Picked up automatically + shared cross-tool.
- **A new reviewer/locator** → add `agents/<name>.md` (frontmatter: `name`,
  `description`, `tools`, `model`).
- **A new always-allow command** → add it to `settings.json` `permissions.allow`
  (scoped, e.g. `Bash(tool:*)`), not the gitignored local file.
- **A new guardrail** → add a hook script here, register it in `settings.json`
  `hooks`, and (if it should apply to Codex too) in `.codex/hooks.json`. Keep
  hooks advisory/best-effort and macOS bash-3.2-safe.

Keep `AGENTS.md` the single source of truth for *guidance*; this directory is for
*machinery*. When they overlap, link rather than duplicate.
