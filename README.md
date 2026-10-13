# ClusterX OS

[![ci](https://github.com/fernandogarzaaa/clusterx-os/actions/workflows/ci.yml/badge.svg)](https://github.com/fernandogarzaaa/clusterx-os/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/fernandogarzaaa/clusterx-os)](https://github.com/fernandogarzaaa/clusterx-os/releases)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Agentic AI OS for lead qualification: engage leads, qualify intent, update CRM, and book
appointments automatically.

ClusterX OS is a self-hosted FastAPI service with a server-rendered dashboard. Leads arrive by
webhook, CSV, WhatsApp or SMS; a qualification agent talks to each one, fills need / budget /
timeline, classifies intent (Hot / Warm / Cold / Unqualified), books a meeting on your calendar
and keeps your CRM in sync. It runs fully offline with a deterministic rule engine; an
OpenAI-compatible LLM is optional.

## Quickstart

### Docker (Postgres + API + worker)

```bash
git clone https://github.com/fernandogarzaaa/clusterx-os.git && cd clusterx-os
cp .env.example .env          # blank secrets fall back to dev defaults
docker compose up -d --build
open http://localhost:8005    # admin@clusterx.local / ChangeMe123!
```

`docker compose` starts `db` (postgres:16), runs `migrate` once, then starts `api` and `worker`.

### Local (SQLite)

```bash
python -m venv .venv && . .venv/bin/activate      # Python 3.11+
pip install -e ".[dev]"
clusterx-os migrate
clusterx-os seed-demo          # optional demo leads, conversations and a campaign
clusterx-os serve --port 8005  # dashboard at http://localhost:8005, API docs at /api/docs
```

Install from a release instead: download the wheel from the
[releases page](https://github.com/fernandogarzaaa/clusterx-os/releases) and
`pip install clusterx_os-1.0.0-py3-none-any.whl`. For reproducible installs use the pinned set:
`pip install -r requirements.lock && pip install --no-deps <wheel>`.

### Codespaces

`Code` → `Codespaces` → `Create codespace on main`. The dev container installs the package,
migrates, loads demo data and serves the dashboard on the forwarded port 8005.

### Send it a lead

```bash
curl -X POST http://localhost:8005/api/webhooks/leads \
  -H "X-API-Key: dev-webhook-key" -H "Idempotency-Key: form-123" \
  -H "Content-Type: application/json" \
  -d '{"name":"Asha","email":"asha@example.com","message":"Hi, we need a CRM for 20 reps, budget $5k, next month"}'
```

The response holds the lead and the agent's first reply. Retrying with the same
`Idempotency-Key` replays the stored response instead of creating a duplicate.

## CLI

| Command | What it does |
| --- | --- |
| `clusterx-os serve [--host --port --workers]` | Run the web/API server (migrates on start when `AUTO_MIGRATE=1`). |
| `clusterx-os worker [--once]` | Run follow-ups, the CRM outbox and housekeeping. Run exactly one per database. |
| `clusterx-os migrate` | Apply Alembic migrations. |
| `clusterx-os seed-demo` | Load demo leads, conversations and a campaign (development only). |
| `clusterx-os create-user` | Create a dashboard/API user (prompts for the password if omitted). |
| `clusterx-os check-config` | Validate the environment and exit non-zero on problems. |

## Configuration

All settings are environment variables (or a `.env` file); [.env.example](.env.example) documents
every one. The ones you will touch first:

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_ENV` | `development` | `production` refuses to start with default secrets or demo data. |
| `DATABASE_URL` | `sqlite:///./data/app.db` | Use `postgresql+psycopg2://…` in production. |
| `SECRET_KEY` | dev default | ≥32 random chars in production (signs session JWTs). |
| `WEBHOOK_API_KEY` | `dev-webhook-key` | `X-API-Key` for `/api/webhooks/leads` and `/messages`; ≥16 chars in production. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | `admin@clusterx.local` / `ChangeMe123!` | First admin, created on an empty database. |
| `BUSINESS_TIMEZONE` | `UTC` | IANA name; availability and parsed times use it. |
| `CALENDAR_PROVIDER` | `local` | `local`, `google` (OAuth refresh token) or `calcom`. |
| `CRM_PROVIDER` | `local` | `local` or `hubspot` (private-app token). |
| `LLM_PROVIDER` | `offline` | `offline`, `mock`, or `openai` for any OpenAI-compatible endpoint (Groq, OpenRouter, Ollama…). |
| `OUTBOUND_CHANNEL` | `simulator` | `simulator`, `twilio` or `whatsapp` (Meta Cloud API). |
| `RUN_SCHEDULER` | `0` | `1` runs the worker inside the API process (single-node setups). |

Blank values in `.env` mean "not set", so a copied `.env.example` boots in development and is
rejected in production until the three secrets are set. Run `clusterx-os check-config` to see
every problem at once.

## Architecture

```
            ┌─────────── inbound ───────────┐
 webhook / CSV / Twilio / WhatsApp ──► FastAPI (clusterx_os.web)
                                          │  auth (JWT cookie / bearer), rate limits,
                                          │  Idempotency-Key, signature checks
                                          ▼
                              services (leads, followups, outbox)
                               │              │              │
                     agent.py (pure)    booking.py      CRM outbox ──► worker ──► HubSpot / local
            rules + optional LLM rewrite  slots, conflicts,
            timeparse.py (natural times)   calendar adapters ──► local / Google / Cal.com
                                          ▼
                       SQLAlchemy 2 models + Alembic migrations (SQLite or Postgres)
```

- **`agent.py`** — deterministic state machine (greet → needs → budget → timeline → intent →
  book | handoff). Extracts budgets (`$5,000`, `2.5 lakh`, `₱80k`) and timelines, classifies
  intent with reasons, and drafts the reply. A configured LLM may rewrite the draft and fill missed
  slots; any LLM failure falls back to the rule result. Opt-outs and booking confirmations never go
  through the LLM. The agent does no I/O except the injected LLM client.
- **`timeparse.py`** — turns "tomorrow 3pm", "Tue 10:30", "Oct 15 2pm" into datetimes relative to
  the current time in `BUSINESS_TIMEZONE`.
- **`booking.py`** — weekly availability templates, slot generation, conflict checks against the
  local table and the external calendar, ICS export.
- **`idempotency.py` / `ratelimit.py`** — stored responses keyed by `Idempotency-Key` + request
  fingerprint (TTL `IDEMPOTENCY_TTL_HOURS`); in-process sliding-window limits for login, webhook
  and API traffic.
- **`worker.py`** — campaign follow-ups after `followup_hours` of silence, CRM outbox delivery with
  retries (`OUTBOX_MAX_ATTEMPTS`), expired-record cleanup.
- **Dashboard** — Jinja templates with self-hosted CSS/JS (no CDN): leads kanban, lead timeline,
  calendar, campaigns, channels, co-pilot Q&A over the pipeline.

Health endpoints: `/healthz` (liveness), `/readyz` (database reachable, 503 otherwise),
`/api/health` (database plus integration status).

## Deploy

1. Provision Postgres 16 and set `DATABASE_URL`.
2. Set `APP_ENV=production`, a random `SECRET_KEY` (`python -c "import secrets; print(secrets.token_urlsafe(48))"`),
   `WEBHOOK_API_KEY`, `ADMIN_PASSWORD`, `COOKIE_SECURE=1` and `PUBLIC_BASE_URL` (needed to verify
   Twilio signatures behind a proxy).
3. Run `clusterx-os migrate` once per release, then the API (`clusterx-os serve`, or the Docker
   image with `AUTO_MIGRATE=0`) and **one** `clusterx-os worker`.
4. Terminate TLS at your proxy and set `FORWARDED_ALLOW_IPS` to the proxy's address/CIDR: the
   image trusts `X-Forwarded-*` headers only from those peers (default `127.0.0.1`), so per-IP
   rate limits and logs see the real client and spoofed headers from direct clients are ignored.

The Docker image is multi-stage, runs as an unprivileged user (uid 10001), stores SQLite data in
the `/data` volume and has a `HEALTHCHECK` on `/healthz`. `docker-compose.yml` is a working
single-host production layout; any Docker host (a VPS, Railway, Render, Fly.io) can run it.

Rate limits are kept in process memory: with several API replicas each replica enforces its own
limit.

## Development

```bash
pip install -e ".[dev]"
ruff check .
mypy                      # strict
pytest                    # SQLite; set TEST_DATABASE_URL to run against Postgres
python -m build           # wheel + sdist into dist/
```

CI runs lint, strict typing, the test suite on Python 3.11–3.13, the suite against Postgres 16,
a package build and a Docker build with a smoke test. Pushing a `v*` tag builds the wheel and
sdist and publishes a GitHub release with the matching CHANGELOG section.

## License

[MIT](LICENSE)
