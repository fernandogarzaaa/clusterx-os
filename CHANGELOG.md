# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-11

First stable release. The prototype `app/` package was rewritten as the installable
`clusterx_os` package.

### Added
- Installable package with a `clusterx-os` CLI: `serve`, `worker`, `migrate`, `seed-demo`,
  `create-user`, `check-config`.
- Lead-qualification agent: deterministic state machine (greet, needs, budget, timeline, intent,
  book or handoff), budget and timeline extraction, intent classification with reasons, opt-out
  handling; optional OpenAI-compatible LLM rewrite with rule-engine fallback.
- Natural-language meeting-time parsing ("tomorrow 3pm", "Tue 10:30", "Oct 15 2pm") in the
  business timezone.
- Booking: weekly availability, slot generation, conflict checks, alternatives when a slot is taken,
  ICS export and private per-user ICS feed.
- Integrations: Google Calendar and Cal.com calendars, HubSpot CRM via a retrying outbox, Twilio
  SMS/WhatsApp and Meta WhatsApp Cloud API with signature verification.
- Background worker for campaign follow-ups, CRM outbox delivery and housekeeping
  (`clusterx-os worker` or `RUN_SCHEDULER=1`).
- `Idempotency-Key` support on webhook and create endpoints; sliding-window rate limits for login,
  webhook and API traffic.
- Alembic migrations for SQLite and PostgreSQL; `/healthz` and `/readyz` probes.
- Structured JSON logging; strict configuration validation that refuses to start in production
  with default secrets or demo data.
- Self-hosted dashboard assets (no CDN).
- Multi-stage, non-root Docker image with a health check; `docker-compose.yml` with Postgres 16,
  a one-shot migrate service, API and worker.
- Documented `.env.example`; blank secret values fall back to the development defaults.
- `requirements.lock` with the fully pinned runtime set.
- Test suite (350 tests) with ruff and strict mypy; mutation testing with testteeth.
- CI: lint, typing, tests on Python 3.11–3.13, tests on Postgres 16, package build, Docker build
  and smoke test. Tag-triggered release workflow publishing wheel and sdist.
- Dependabot for pip and GitHub Actions, SECURITY.md, this changelog.

### Changed
- Python 3.11+ is required; dependencies are pinned.

### Fixed
- `clusterx-os serve --workers N` validates config, migrates and bootstraps once in the parent
  process, so workers no longer race to create the schema on a fresh database.
- Calendar slot test no longer fails when CI runs on a Sunday.

[Unreleased]: https://github.com/fernandogarzaaa/clusterx-os/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/fernandogarzaaa/clusterx-os/releases/tag/v1.0.0
