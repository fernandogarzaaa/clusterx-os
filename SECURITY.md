# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | Yes |
| < 1.0 | No |

## Reporting a vulnerability

Please report security issues privately, either through GitHub's
[private vulnerability reporting](https://github.com/fernandogarzaaa/clusterx-os/security/advisories/new)
or by email to **fernandogarzaaa@gmail.com**. Do not open a public issue for a vulnerability.

Include steps to reproduce, the affected version or commit, and the impact if known. You should
get an initial response within a week, and a fix or mitigation plan once the report is confirmed.

## Deployment hardening

- Run with `APP_ENV=production`: startup is refused while `SECRET_KEY`, `WEBHOOK_API_KEY` or
  `ADMIN_PASSWORD` keep their development defaults, or while demo data is enabled.
- Serve over HTTPS and set `COOKIE_SECURE=1`; expose the app only behind a trusted proxy.
- Set `TWILIO_VALIDATE_SIGNATURE=1` (default) and `WHATSAPP_APP_SECRET` so inbound channel
  webhooks are signature-checked.
- Rate limits are per process; add a shared limit at the proxy for strict global limits.
- Never commit `.env` files; they are git-ignored.
