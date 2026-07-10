# spin-payments

A Django payments-orchestration service: one API-key-authenticated gateway that
fronts multiple payment providers (Stripe, M-Pesa Daraja, …). External systems
initiate payments against chargeable events; the service routes each to the right
provider account, tracks a `PaymentIntent → Transaction` lifecycle with a
double-entry ledger, runs provider calls asynchronously via Celery, reconciles
pending transactions, and delivers signed webhooks back to the calling system.

> **Working on this repo (human or AI)?** Read [`AGENTS.md`](AGENTS.md) — the
> architecture, invariants, and workflow live there. This README is the quickstart.

## Stack

- **Python 3.14** managed with **[uv](https://docs.astral.sh/uv/)**
- **Django 5.2** — function-based views + custom gateway middleware (no DRF)
- **Celery 5.6** + **RabbitMQ** broker, **django-celery-beat** schedule
- **PostgreSQL 18** (deployed) / **SQLite** (local fallback)
- **ruff** (lint + format), **gunicorn**, **whitenoise**
- Docker → GitLab registry → **Helm** → **ArgoCD**; secrets from **Vault**

## Quickstart

```sh
# 1. Install deps into .venv
make install

# 2. Configure (optional — SQLite is used when DATABASE_DB is unset)
cp .env.example .env

# 3. Migrate + run
make migrate
make run                 # http://localhost:8000

# health check
curl http://localhost:8000/healthz
```

Full stack (app + Celery worker + Postgres + RabbitMQ) via Docker:

```sh
make up                  # start;  make logs / make ps / make down
make d-migrate           # migrate inside the container
```

Async work needs RabbitMQ running:

```sh
make worker              # Celery worker (payments.high, payments.low)
make beat                # Celery beat scheduler
```

## Quality gates

```sh
make lint                # ruff check + ruff format --check (no changes)
make fix                 # ruff check --fix + ruff format
make test                # Django test suite
make check               # lint + test — run this before every PR (mirrors CI)
```

Run `make help` for the full target list.

## Layout

| Path             | What                                                            |
| ---------------- | -------------------------------------------------------------- |
| `spin_payments/` | Django project (settings, urls, celery, wsgi/asgi)             |
| `core/`          | Payment domain — models, views, providers, services, tasks     |
| `api/`           | HTTP gateway middleware + rate-limit models                    |
| `audit/`         | Request/audit logging + per-request context                    |
| `base/`          | Shared `BaseModel`, `/healthz`                                  |
| `utils/`         | JSON response envelope, request helpers                         |
| `helm/`          | Helm values for stage / prod                                   |

## Deploy

`develop` auto-deploys to **stage**, `main` auto-deploys to **prod** (GitHub
Actions → GitLab registry → Helm template → ArgoCD). Both branches are protected:
every change lands on a feature branch and merges via a reviewed PR. See
[`AGENTS.md`](AGENTS.md) for the branch workflow.
