# ClusterX OS

Agentic AI OS for lead qualification: engage leads, qualify intent, update CRM, and book appointments automatically. (Beta)

## Quickstart

```bash
cp .env.example .env
docker compose up --build
# open http://localhost:8005
# login: admin@clusterx.local / ChangeMe123!
```

## Live demo

**One-click cloud demo (no local setup):** open this repo in GitHub Codespaces
(`Code` -> `Codespaces` -> `Create codespace on main`). The dev container boots
the app + Postgres via docker compose, seeds demo data on first start, and
forwards the app port automatically. Log in with `admin@clusterx.local` /
`ChangeMe123!`.

For a 24/7 public demo, deploy `docker-compose.yml` to any host that runs
Docker (a VPS, Railway, Render, or Fly.io) and point your domain at the app
port.

Local dev (SQLite):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.seed
uvicorn app.main:app --port 8005
```

## Tests

```bash
python -m pytest -q
ruff check app
```

## What it does

ClusterX OS is an agentic lead-qualification OS:

- **Leads**: manual add, CSV import (`POST /api/leads/import-csv`), and an
  inbound webhook `POST /api/webhooks/leads` authed with the `X-API-Key`
  header. The key is `WEBHOOK_API_KEY` (default `dev-webhook-key`; change it
  in production).
- **AI qualification agent**: every lead gets a WhatsApp-style thread. The
  agent walks a state machine (greet, needs, budget, timeline, intent,
  book/handoff), filling slots from free text (budget amounts like `$5,000`
  or `2.5 lakh`, timeline phrases like `next month`) and composing replies
  from the current slot state. When `LLM_BASE_URL` + `LLM_API_KEY` are set,
  an OpenAI-compatible model drafts replies; otherwise the local
  rule-based responder handles everything.
- **Intent classification**: Hot / Warm / Cold / Unqualified with confidence
  and the matched phrases as reasons, shown as a badge on each lead.
- **CRM kanban**: New, Contacted, Qualified, Booked, Won, Lost. Lead detail
  shows the conversation, intent signals, and a timeline of events.
- **Calendar**: weekday availability templates, conflict-checked booking,
  per-booking ICS download, and a per-user private ICS feed URL
  (`/api/calendar/feed/<token>.ics`, shown on the Calendar page).
- **Campaigns**: create follow-up campaigns with `{name}`/`{company}`
  templates; leads are enrolled and nudged after `followup_hours` of
  silence. The APScheduler job runs only with `RUN_SCHEDULER=1`
  (every 60s); `POST /api/campaigns/<id>/run-now` executes the rule
  on demand.
- **Co-pilot**: natural-language Q&A over the pipeline, e.g.
  "how many hot leads?", "show leads from last week", "leads with no reply".
- **Channels**: the in-app simulator always works. Twilio and WhatsApp are
  real httpx adapters that need credentials (`TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` / `WHATSAPP_TOKEN`,
  `WHATSAPP_PHONE_NUMBER_ID`); without them they raise a clear
  ConfigurationError instead of pretending to send. See the Channels page.

## License

MIT
