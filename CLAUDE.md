# AGENTS.md

Guidance for AI agents (Claude, Codex, Cursor, …) working on this repo. Keep
edits to this file additive and minimal. This is the root of an **AGENTS.md
tree**; `CLAUDE.md` symlinks to it so every tool reads the same brief.

## What this is

**spin-payments** is a Django payments-orchestration service: a single gateway
that fronts multiple payment providers (Stripe, M-Pesa Daraja, …) behind one
API-key-authenticated HTTP API. External **systems** initiate payments against
**chargeable events**; the service routes each to the right **provider account**,
records a **PaymentIntent** → **Transaction** lifecycle with a double-entry
ledger, runs provider calls **asynchronously via Celery**, **reconciles** pending
transactions, and **delivers signed webhooks** back to the calling system.

It is a backend service only — no user-facing web UI. The only HTML surface is
the Django admin at `/cia/`.

## Stack pins

- **Python 3.14** (`.python-version`), managed with **uv** (`uv.lock` is
  committed; always `uv sync --frozen` / `uv run …`, never bare `pip`).
- **Django 5.2**. Plain function-based views + custom middleware — **no DRF**.
- **Celery 5.6** with a **RabbitMQ** broker; **django-celery-beat** for the
  schedule (DB-backed `DatabaseScheduler`). Two queues: `payments.high`
  (money-movement) and `payments.low` (reconcile / webhooks / scans).
- **PostgreSQL 18** in every deployed env; **SQLite** fallback for local dev and
  tests (used automatically when `DATABASE_DB` is unset — see `settings.py`).
- **Stripe** SDK; **requests** for other providers. **gunicorn** in prod,
  **whitenoise** for static, **django-cors-headers**.
- **ruff** for lint + format (line length 100, `target-version = py314`; config
  in `pyproject.toml`).
- Deploy: **Docker** image → GitLab registry → **Helm** chart (`spinm/app`
  v1.3.1) → **ArgoCD**. Secrets come from **Vault** via External Secrets.

## Repo layout

```
payments/     Django project: settings.py, urls.py, celery.py, wsgi/asgi
core/              Payment domain — the heart of the service
  models.py          System, Provider, ProviderAccount, ChargeableEvent,
                     PaymentIntent, Transaction, TransactionStateLog,
                     LedgerAccount, LedgerPosting, WebhookOutbox,
                     ProviderCallbackLog, ReconciliationRecord, …
  views.py           initiate / capture / void / refund / provider_callback
  urls.py            routes under /api/v1/core/…
  providers/         BaseProvider ABC + stripe / mpesa_daraja implementations
  services/          registry (provider lookup), executor (result→status),
                     payment_services (PaymentServices facade)
  tasks.py           Celery tasks (charge/authorize/capture/void/refund,
                     reconcile, webhook delivery, periodic scans)
api/               HTTP gateway: GatewayControlMiddleware + rate-limit models
  middleware/gateway.py  API-key auth, IP allowlist, rate limiting, req logging
  models.py          RateLimitRule / RateLimitAttempt / RateLimitBlock
  urls.py            /api/v1/ → includes core.urls
audit/             RequestLog, AuditLog, RequestContext (per-request state)
base/              BaseModel (shared PK/timestamps/audit), healthz view
utils/             ResponseProvider (JSON envelope), ExtendedRequest, common helpers
helm/              stage.yml / prod.yml — Helm values (image, probes, secrets, celery)
.github/workflows/ test.yml (CI) · stage.yml (deploy stage) · prod.yml (deploy prod)
architecture.txt   ASCII ER sketch of the provider/account/transaction model
```

## How to run

`make help` lists every target. The common loop:

```
make install        # uv sync (runtime + dev deps into .venv)
make run            # Django dev server on :8000 (SQLite unless DATABASE_DB set)
make migrate        # apply migrations
make makemigrations # after model changes
make worker         # Celery worker (needs a running RabbitMQ)
make beat           # Celery beat scheduler
make shell          # Django shell
make superuser      # create a /cia admin user

make lint           # ruff check + ruff format --check   (no changes)
make fix            # ruff check --fix + ruff format      (auto-fix)
make test           # Django test suite
make check          # lint + test — the local pre-commit gate (== CI)

make up             # full stack via docker compose: app + worker + postgres + rabbitmq
make down / logs / ps
make d-migrate      # migrate inside the app container
```

Local config is env-driven; copy `.env.example` to `.env` for overrides. With no
`DATABASE_DB`, the app uses `db.sqlite3` so you can run without Postgres. Celery
work needs RabbitMQ (`make up` provides it, management UI on :15672). RabbitMQ
management UI and Postgres are exposed on the usual ports by `docker compose`.

## Architecture — the request lifecycle

1. **Gateway (`api/middleware/gateway.py`).** Every request except `/health*`
   passes through `GatewayControlMiddleware`, which:
   - authenticates the caller by the **`X-Api-Key`** header (SHA-256 hashed and
     matched against `System.hashed_api_key`), enforces the System's IP
     allowlist, and attaches `request.api_client`;
   - applies **DB-backed rate limiting** (`RateLimitRule` → `Attempt`/`Block`,
     with `X-RateLimit-*` / `Retry-After` response headers);
   - records a **`RequestLog`** (via `audit.services.request_context`), sanitising
     secrets.
   Exempt paths (no API key): `/cia` (admin), `/static`, `/media`, provider
   `callbacks`, `favicon`. **Health checks (`/health*`) bypass the middleware
   entirely** — no API-key check, no rate-limit DB queries, no logging (a probe
   must not couple liveness to the DB).

2. **Views (`core/views.py`).** Thin function views (`@require_http_methods`).
   They read the parsed body via `request.data` (set by the middleware), delegate
   to **`PaymentServices`**, and return through **`ResponseProvider`** (a uniform
   `{success, message, data}` JSON envelope). Views do **not** call providers
   directly — they **queue Celery tasks** (`queue_capture`, etc.).

3. **Services + providers.** `PaymentServices` (`core/services/payment_services.py`)
   is the facade for all money movement. Provider integrations subclass
   **`BaseProvider`** (`core/providers/base_provider.py`) and are looked up through
   the **registry** (`get_provider_instance(class_name, credentials, config)`);
   every provider call returns a **`ProviderResult`** which `executor.py` maps onto
   `Transaction` / `PaymentIntent` statuses.

4. **Async (`core/tasks.py`).** Provider I/O runs in Celery tasks on
   `payments.high`; reconciliation, webhook delivery, and periodic scans run on
   `payments.low`. Tasks are idempotent and carry retry/backoff policies.
   Inbound provider **callbacks** are persisted as `ProviderCallbackLog` and
   processed by a task. Outbound **webhooks** go through a `WebhookOutbox` with
   HMAC-signed payloads and exponential backoff.

## Agent tooling

Shared, version-controlled agent config lives in `.claude/` (see
[`.claude/README.md`](.claude/README.md)). `CLAUDE.md` symlinks to this file and
`.agents/skills` symlinks to `.claude/skills`, so Claude Code, Codex, and other
tools all read the same brief, skills, and guardrails. `.codex/` mirrors the
hooks + MCP wiring for the Codex CLI.

- **Project skills** (invoke as `/<name>`): `/add-provider` (new payment
  provider), `/add-endpoint` (new gateway API endpoint), `/add-model` (model +
  migration + admin), `/add-task` (new Celery task). Each encodes the end-to-end
  recipe **with the invariants** for that kind of change. Generic skills like
  `/code-review`, `/security-review`, and `/pr` ship with the harness.
- **Subagents** (`.claude/agents/`): `django-backend-reviewer` and
  `payment-flow-reviewer` review a diff against the conventions and the
  money-movement invariants below; `codebase-locator` maps where a feature lives.
  Hand off before a PR, e.g. "use the payment-flow-reviewer on my changes".
- **Guardrails (hooks)**: `PreToolUse` refuses `git commit`/`git push` on the
  protected branches `main` and `develop` (`guard-branch.sh`) and blocks a
  `gh pr create/edit` that carries an AI-attribution footer (`guard-pr.sh`);
  `PostToolUse` auto-formats edited `.py` with ruff (`format-on-save.sh`); `Stop`
  runs an advisory `ruff check` over changed files (`lint-on-stop.sh`);
  `SessionStart` prints a short orientation.
- **Permissions & MCP**: `.claude/settings.json` allow-lists the everyday tools
  (uv, make, git, gh, ruff, docker compose, python/manage.py, psql, ripgrep) so
  routine commands don't prompt, and **denies reading `.env` secrets**;
  `.mcp.json` wires **context7** for up-to-date Django/Celery/Stripe library docs.

## Workflow

- **`develop` and `main` are protected.** Pushing to `develop` **auto-deploys to
  stage**; pushing to `main` **auto-deploys to prod** (see
  `.github/workflows/stage.yml` / `prod.yml`). Never commit or push directly to
  either. **Every change lands on a feature branch and merges via a reviewed PR.**
  The flow is: `feature branch → PR into develop → (stage) → PR develop→main →
  (prod)`. Humans/CI merge; **agents do not merge their own PRs**. If you find
  yourself on `main`/`develop` with local changes, branch first
  (`git switch -c fix/<topic>`), then open a PR.
- **Branch naming**: `feat/…`, `fix/…`, `chore/…`, `docs/…` (or `SPIN-###/…` for
  a ticket).
- **Commit messages**: imperative subject, blank line, body explaining **why**
  (the diff shows what). End with the co-author trailer when an agent contributed
  (use the agent's actual model), e.g.
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **PRs follow [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)**
  (Context / Changes Made / Testing). **Do NOT put any AI-attribution footer** —
  no "Generated with Claude Code" line, no `claude.ai/code` session link — in the
  PR **title or body** (the co-author trailer belongs in the *commit*). A hook
  blocks it.
- **Green before merge.** CI (`test.yml`, runs on PRs to `main`/`develop`) runs
  ruff lint + format checks, `py_compile`, Django system checks (incl.
  `--tag security`), WSGI/ASGI load, `collectstatic --dry-run`, migrate, a live
  `/healthz` probe, a Docker build + `manage.py check`, and the test suite. Run
  `make check` locally first.

## Things easy to get wrong (payments invariants)

- **Money movement is idempotent.** `PaymentIntent`/`Transaction` carry an
  `idempotency_key`; re-initiating with the same key must not double-charge.
  Provider calls run in Celery tasks that can retry — a retried task must not
  create a second charge. Guard new flows with the key + the transaction state
  machine, never by "checking if it looks done".
- **Never call a provider from a view.** Views validate + queue; the Celery task
  (via `PaymentServices`) does the I/O and writes the `Transaction`. Keep request
  latency off the provider's network path.
- **Status transitions go through the executor/state machine.** Map a
  `ProviderResult` onto `Transaction`/`PaymentIntent` status via `executor.py`
  (`txn_status_from_result`, `intent_status_after_txn`) and record a
  `TransactionStateLog`. Don't set `status = "SUCCESS"` ad hoc.
- **The ledger is double-entry.** Balance changes are `LedgerPosting`s against
  `LedgerAccount`s. Don't invent a shortcut "balance" field on another model.
- **Callbacks are untrusted.** Verify every inbound provider callback
  (`BaseProvider.verify_callback`) before acting on it; persist it as
  `ProviderCallbackLog` and process asynchronously and idempotently.
- **Outbound webhooks are signed + retried.** Enqueue via `WebhookOutbox`
  (HMAC-SHA256 with `System.webhook_secret`, exponential backoff) — never fire a
  bare `requests.post` to a system's `webhook_url` inline.
- **Secrets never get logged or echoed.** API keys, `webhook_secret`, provider
  credentials, Vault-sourced env — sanitize before logging or returning
  (`utils.common.sanitize_data`, already applied in the request logger). Grep new
  code near a logger or response for `secret`/`key`/`token`/`password`.
- **New middleware-exempt paths are a security decision.** Adding a path to the
  gateway's exempt lists bypasses API-key auth and/or rate limiting — justify it
  in the PR.
- **Migrations must be reviewed and reversible.** Run `make makemigrations`,
  commit the generated file, and keep it applying cleanly (`make migrate`). Ruff
  ignores `**/migrations/*` — don't hand-edit generated migrations to satisfy the
  linter.

## When in doubt

Ask. Don't invent. Especially for: new top-level dependencies, schema/model
changes, provider-contract changes, new gateway-exempt paths, anything touching
idempotency / the ledger / reconciliation, or hosting/deploy changes.
