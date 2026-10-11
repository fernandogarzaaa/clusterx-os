"""HTTP integration tests through the FastAPI app (TestClient, SQLite, offline adapters)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from clusterx_os.booking import next_open_slots
from clusterx_os.config import get_settings
from clusterx_os.integrations.channels import twilio_signature
from clusterx_os.models import Lead, Message, OutboxJob, utcnow
from tests.conftest import ADMIN, make_client

# ------------------------------------------------------------ system ---


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ok", "database": True}
    body = client.get("/api/health").json()
    assert body == {"status": "ok", "app": "ClusterX OS", "version": "1.0.0", "database": True}


def test_security_headers_and_request_id(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in r.headers
    assert len(client.get("/healthz").headers["X-Request-ID"]) == 32


def test_hsts_when_cookie_secure() -> None:
    with make_client(COOKIE_SECURE=True) as c:
        assert c.get("/healthz").headers["Strict-Transport-Security"].startswith("max-age=")


def test_body_size_limit(client: TestClient, auth: dict[str, str]) -> None:
    big = "x" * 2_100_000
    r = client.post("/api/leads", headers={**auth, "Content-Type": "application/json"},
                    content=json.dumps({"name": big}))
    assert r.status_code == 413


def test_openapi_and_static(client: TestClient) -> None:
    assert client.get("/api/openapi.json").json()["info"]["version"] == "1.0.0"
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/chart.umd.min.js").status_code == 200


def test_production_refuses_insecure_defaults() -> None:
    from clusterx_os.web.app import StartupError

    c = make_client(APP_ENV="production", SECRET_KEY="dev-insecure-secret-key-change-me-in-production")
    try:
        with c:
            raise AssertionError("startup should fail")
    except StartupError as exc:
        assert "SECRET_KEY" in str(exc)


# -------------------------------------------------------------- auth ---


def test_login_api_and_me(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code == 401
    bad = client.post("/api/auth/login", json={"email": ADMIN["email"], "password": "nope"})
    assert bad.status_code == 401
    unknown = client.post("/api/auth/login", json={"email": "x@y.example", "password": "nope"})
    assert unknown.status_code == 401
    r = client.post("/api/auth/login", json={"email": "ADMIN@clusterx.local",
                                             "password": ADMIN["password"]})
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"
    assert "access_token" in r.cookies
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + r.json()["access_token"]})
    assert me.json()["role"] == "admin"
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    out = client.post("/api/auth/logout")
    assert out.json() == {"ok": True}


def test_login_form_and_pages(client: TestClient) -> None:
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/login").status_code == 200
    bad = client.post("/login", data={"email": ADMIN["email"], "password": "x"},
                      follow_redirects=False)
    assert bad.headers["location"] == "/login?error=1"
    assert "Invalid email or password" in client.get("/login?error=1").text
    r = client.post("/login", data=ADMIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    lead = client.post("/api/leads", json={"name": "Page Lead"}).json()
    for path in ["/", "/?range=24h", "/leads", f"/leads/{lead['id']}", "/calendar",
                 "/calendar?date=2026-10-14", "/campaigns", "/copilot", "/channels"]:
        page = client.get(path)
        assert page.status_code == 200, path
        assert "cdn.tailwindcss.com" not in page.text
    assert client.get("/leads/999999").status_code == 404
    assert client.get("/logout", follow_redirects=False).headers["location"] == "/login"


def test_login_rate_limit() -> None:
    with make_client(LOGIN_RATE_LIMIT_PER_MINUTE=2) as c:
        for _ in range(2):
            assert c.post("/api/auth/login", json={"email": "a@b.example",
                                                   "password": "x"}).status_code == 401
        r = c.post("/api/auth/login", json={"email": "a@b.example", "password": "x"})
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) >= 1


def test_user_admin(client: TestClient, auth: dict[str, str]) -> None:
    weak = client.post("/api/users", headers=auth, json={"email": "m@x.example",
                                                         "password": "short"})
    assert weak.status_code == 422
    r = client.post("/api/users", headers=auth, json={"email": "M@X.example",
                                                      "password": "member-pass-1", "name": "M"})
    assert r.status_code == 201 and r.json()["email"] == "m@x.example"
    dup = client.post("/api/users", headers=auth, json={"email": "m@x.example",
                                                        "password": "member-pass-1"})
    assert dup.status_code == 409
    assert len(client.get("/api/users", headers=auth).json()["users"]) == 2
    tok = client.post("/api/auth/login", json={"email": "m@x.example",
                                               "password": "member-pass-1"}).json()
    member = {"Authorization": "Bearer " + tok["access_token"]}
    assert client.get("/api/users", headers=member).status_code == 403
    me_id = client.get("/api/auth/me", headers=auth).json()["id"]
    assert client.post(f"/api/users/{me_id}/deactivate", headers=auth).status_code == 400
    uid = r.json()["id"]
    assert client.post(f"/api/users/{uid}/deactivate", headers=auth).json()["is_active"] is False
    assert client.get("/api/auth/me", headers=member).status_code == 401
    assert client.post("/api/users/9999/deactivate", headers=auth).status_code == 404


# ------------------------------------------------------------- leads ---


def test_lead_crud_and_validation(client: TestClient, auth: dict[str, str]) -> None:
    client.cookies.clear()
    assert client.get("/api/leads").status_code == 401
    bad = client.post("/api/leads", headers=auth, json={"email": "not-an-email"})
    assert bad.status_code == 422
    assert client.post("/api/leads", headers=auth, json={"phone": "12"}).status_code == 422
    assert client.post("/api/leads", headers=auth, json={"company": "x"}).status_code == 422
    assert client.post("/api/leads", headers=auth, json={"name": "a", "evil": 1}).status_code == 422
    r = client.post("/api/leads", headers=auth, json={"name": "Ann Lee", "email": "Ann@X.Example",
                                                      "phone": "+1 555 000 1111"})
    assert r.status_code == 201
    lead = r.json()
    assert (lead["email"], lead["phone"], lead["stage"]) == ("ann@x.example", "+15550001111",
                                                             "New")
    lid = lead["id"]
    assert client.get(f"/api/leads/{lid}", headers=auth).json()["events"][0]["kind"] == \
        "lead_created"
    patched = client.patch(f"/api/leads/{lid}", headers=auth, json={"company": "Acme"}).json()
    assert patched["company"] == "Acme"
    assert client.patch(f"/api/leads/{lid}", headers=auth,
                        json={"email": "bad"}).status_code == 422
    stage = client.post(f"/api/leads/{lid}/stage", headers=auth, json={"stage": "Won"}).json()
    assert stage["stage"] == "Won"
    assert client.post(f"/api/leads/{lid}/stage", headers=auth,
                       json={"stage": "Nope"}).status_code == 422
    listing = client.get("/api/leads?q=acme", headers=auth).json()
    assert listing["total"] == 1
    assert client.get("/api/leads?stage=New", headers=auth).json()["total"] == 0
    assert client.get("/api/leads?intent=Hot", headers=auth).json()["total"] == 0
    assert client.get("/api/leads/424242", headers=auth).status_code == 404
    assert client.delete(f"/api/leads/{lid}", headers=auth).status_code == 204
    assert client.get(f"/api/leads/{lid}", headers=auth).status_code == 404


def test_lead_pagination(client: TestClient, auth: dict[str, str]) -> None:
    for i in range(5):
        client.post("/api/leads", headers=auth, json={"name": f"L{i}"})
    page = client.get("/api/leads?limit=2&offset=1", headers=auth).json()
    assert page["total"] == 5 and len(page["leads"]) == 2
    assert [x["name"] for x in page["leads"]] == ["L3", "L2"]
    assert client.get("/api/leads?limit=500", headers=auth).status_code == 422


def test_create_lead_idempotency(client: TestClient, auth: dict[str, str]) -> None:
    h = {**auth, "Idempotency-Key": "abc-1"}
    first = client.post("/api/leads", headers=h, json={"name": "Once"})
    second = client.post("/api/leads", headers=h, json={"name": "Once"})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert client.get("/api/leads", headers=auth).json()["total"] == 1
    conflict = client.post("/api/leads", headers=h, json={"name": "Different"})
    assert conflict.status_code == 422
    blank = client.post("/api/leads", headers={**auth, "Idempotency-Key": " "},
                        json={"name": "x"})
    assert blank.status_code == 400


def test_csv_import(client: TestClient, auth: dict[str, str]) -> None:
    csv_body = ("Name,Email,Phone,Company\n"
                "Ann,ann@x.example,+15550001111,Acme\n"
                "Bo,bo@x.example,,Beta\n"
                ",,,\n"
                "Bad,not-an-email,,\n"
                "Ann Again,ANN@x.example,,AcmeCo\n")
    r = client.post("/api/leads/import-csv", headers=auth,
                    files={"file": ("leads.csv", csv_body, "text/csv")})
    body = r.json()
    assert (body["imported"], body["updated"]) == (2, 1)
    assert body["errors"][0]["line"] == 5
    assert client.get("/api/leads", headers=auth).json()["total"] == 2


def test_csv_import_too_large() -> None:
    with make_client(MAX_UPLOAD_BYTES=1024) as c:
        tok = c.post("/api/auth/login", json=ADMIN).json()["access_token"]
        r = c.post("/api/leads/import-csv", headers={"Authorization": f"Bearer {tok}"},
                   files={"file": ("l.csv", "name\n" + "x\n" * 800, "text/csv")})
        assert r.status_code == 413


def test_simulator_conversation_books_meeting(client: TestClient, auth: dict[str, str],
                                              db: Session) -> None:
    lid = client.post("/api/leads", headers=auth, json={"name": "Ann", "email": "a@x.example"}
                      ).json()["id"]
    for text in ["We need a CRM", "$5k", "next month", "spreadsheets"]:
        client.post(f"/api/leads/{lid}/messages", headers=auth, json={"text": text})
    offer = client.post(f"/api/leads/{lid}/messages", headers=auth, json={"text": "yes"}).json()
    assert offer["agent_stage"] == "book"
    slot = next_open_slots(db, get_settings().tz, 30, 14, count=1,
                           after=utcnow() + timedelta(days=1))[0][0]
    text = f"{slot:%Y-%m-%d %H:%M}"
    booked = client.post(f"/api/leads/{lid}/messages", headers=auth, json={"text": text}).json()
    assert booked["appointment_id"] is not None
    assert booked["lead"]["stage"] == "Booked"
    msgs = client.get(f"/api/leads/{lid}/messages", headers=auth).json()["messages"]
    assert len(msgs) == 12 and msgs[-1]["text"].startswith("You're booked Ann")
    appts = client.get("/api/appointments", headers=auth).json()["appointments"]
    assert appts[0]["lead_id"] == lid


def test_manual_send(client: TestClient, auth: dict[str, str]) -> None:
    lid = client.post("/api/leads", headers=auth, json={"name": "A"}).json()["id"]
    r = client.post(f"/api/leads/{lid}/send", headers=auth, json={"text": "Hi from a human"})
    assert r.json()["channel"] == "simulator" and r.json()["delivery_status"] == "sent"
    client.post(f"/api/leads/{lid}/messages", headers=auth, json={"text": "stop"})
    assert client.post(f"/api/leads/{lid}/send", headers=auth,
                       json={"text": "x"}).status_code == 409


def test_dashboard_stats(client: TestClient, auth: dict[str, str]) -> None:
    lid = client.post("/api/leads", headers=auth, json={"name": "A"}).json()["id"]
    client.post(f"/api/leads/{lid}/messages", headers=auth, json={"text": "send pricing"})
    client.post("/api/leads", headers=auth, json={"name": "B"})
    stats = client.get("/api/dashboard/stats?range=24h", headers=auth).json()
    assert stats["total_leads"] == 2
    assert stats["intent_counts"]["Hot"] == 1 and stats["intent_counts"]["Unclassified"] == 1
    assert stats["outcome_counts"] == {"Won": 0, "Lost": 0, "In Pipeline": 2}
    assert client.get("/api/dashboard/stats?range=1y", headers=auth).status_code == 422


# ---------------------------------------------------------- webhooks ---


def test_webhook_lead_auth_and_upsert(client: TestClient, webhook_key: dict[str, str]) -> None:
    assert client.post("/api/webhooks/leads", json={"name": "x"}).status_code == 401
    assert client.post("/api/webhooks/leads", headers={"X-API-Key": "wrong"},
                       json={"name": "x"}).status_code == 401
    r = client.post("/api/webhooks/leads", headers=webhook_key,
                    json={"name": "Web Lead", "email": "w@x.example", "source": "typeform",
                          "utm_campaign": "ignored-extra-field"})
    assert r.status_code == 201 and r.json()["created"] is True
    assert r.json()["lead"]["source"] == "typeform"
    again = client.post("/api/webhooks/leads", headers=webhook_key,
                        json={"email": "W@x.example", "phone": "+15550001111"})
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["lead"]["phone"] == "+15550001111"
    assert client.post("/api/webhooks/leads", headers=webhook_key,
                       json={"company": "x"}).status_code == 422
    assert client.post("/api/webhooks/leads", headers=webhook_key,
                       json={"name": "x", "source": "bad source!"}).status_code == 422


def test_webhook_lead_with_message_and_idempotency(client: TestClient,
                                                   webhook_key: dict[str, str]) -> None:
    h = {**webhook_key, "Idempotency-Key": "form-123"}
    payload = {"name": "Ann", "email": "a@x.example", "message": "We want a demo next month"}
    r1 = client.post("/api/webhooks/leads", headers=h, json=payload)
    r2 = client.post("/api/webhooks/leads", headers=h, json=payload)
    assert r1.json() == r2.json()
    assert r1.json()["reply"].startswith("Thanks Ann")
    assert r1.json()["lead"]["intent"] == "Hot"
    assert r1.json()["lead"]["stage"] == "Qualified"


def test_webhook_rate_limit() -> None:
    with make_client(WEBHOOK_RATE_LIMIT_PER_MINUTE=1) as c:
        h = {"X-API-Key": "test-webhook-key-123456"}
        assert c.post("/api/webhooks/leads", headers=h, json={"name": "a"}).status_code == 201
        assert c.post("/api/webhooks/leads", headers=h, json={"name": "b"}).status_code == 429


def test_webhook_messages(client: TestClient, webhook_key: dict[str, str]) -> None:
    r = client.post("/api/webhooks/messages", headers=webhook_key,
                    json={"email": "m@x.example", "name": "Mo", "text": "need a website",
                          "external_id": "ext-1"})
    body = r.json()
    assert body["duplicate"] is False and body["agent_stage"] == "budget"
    dup = client.post("/api/webhooks/messages", headers=webhook_key,
                      json={"lead_id": body["lead_id"], "text": "need a website",
                            "external_id": "ext-1"}).json()
    assert dup == {"lead_id": body["lead_id"], "duplicate": True, "reply": None,
                   "agent_stage": None}
    assert client.post("/api/webhooks/messages", headers=webhook_key,
                       json={"text": "orphan"}).status_code == 422
    assert client.post("/api/webhooks/messages", headers=webhook_key,
                       json={"lead_id": 98765, "text": "x"}).status_code == 404
    assert client.post("/api/webhooks/messages", headers=webhook_key,
                       json={"email": "m@x.example", "text": ""}).status_code == 422


def test_twilio_inbound_signed(db: Session) -> None:
    with make_client(TWILIO_AUTH_TOKEN="tw-token", PUBLIC_BASE_URL="https://app.example") as c:
        form = {"From": "+15551234567", "Body": "Hi, we need a booking bot", "MessageSid": "SM9",
                "ProfileName": "Tess"}
        url = "https://app.example/api/webhooks/twilio"
        assert c.post("/api/webhooks/twilio", data=form).status_code == 403
        sig = twilio_signature("tw-token", url, form)
        r = c.post("/api/webhooks/twilio", data=form, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
        r2 = c.post("/api/webhooks/twilio", data=form, headers={"X-Twilio-Signature": sig})
        assert r2.status_code == 200
    lead = db.scalars(select(Lead).where(Lead.phone == "+15551234567")).one()
    assert lead.name == "Tess" and lead.source == "twilio" and lead.preferred_channel == "twilio"
    inbound = db.scalars(select(Message).where(Message.lead_id == lead.id,
                                               Message.role == "lead")).all()
    assert len(inbound) == 1  # retry deduplicated by MessageSid
    reply = db.scalars(select(Message).where(Message.lead_id == lead.id,
                                             Message.role == "agent")).one()
    assert reply.channel == "twilio" and reply.delivery_status == "failed"  # no outbound creds


def test_twilio_inbound_unsigned_mode_and_bad_sender(db: Session) -> None:
    with make_client(TWILIO_VALIDATE_SIGNATURE=False) as c:
        assert c.post("/api/webhooks/twilio", data={"From": "junk", "Body": "x"}).status_code == 200
        assert c.post("/api/webhooks/twilio", data={"From": "+15550000001"}).status_code == 200
        assert c.post("/api/webhooks/twilio", data={"From": "", "Body": "x"}).status_code == 200
    assert db.scalars(select(Lead)).all() == []


def test_whatsapp_verify_and_inbound(db: Session) -> None:
    with make_client(WHATSAPP_VERIFY_TOKEN="verify-me", WHATSAPP_APP_SECRET="app-secret") as c:
        ok = c.get("/api/webhooks/whatsapp", params={"hub.mode": "subscribe",
                                                      "hub.verify_token": "verify-me",
                                                      "hub.challenge": "42"})
        assert ok.status_code == 200 and ok.text == "42"
        assert c.get("/api/webhooks/whatsapp", params={"hub.mode": "subscribe",
                                                        "hub.verify_token": "no",
                                                        "hub.challenge": "42"}).status_code == 403
        payload = {"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "15557654321", "profile": {"name": "Wes"}}],
            "messages": [{"from": "15557654321", "id": "wamid.X", "type": "text",
                          "text": {"body": "Looking for pricing"}}]}}]}]}
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
        assert c.post("/api/webhooks/whatsapp", content=raw).status_code == 403
        r = c.post("/api/webhooks/whatsapp", content=raw,
                   headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        assert r.json() == {"received": 1}
        bad = b"not json"
        bad_sig = "sha256=" + hmac.new(b"app-secret", bad, hashlib.sha256).hexdigest()
        assert c.post("/api/webhooks/whatsapp", content=bad,
                      headers={"X-Hub-Signature-256": bad_sig}).status_code == 400
    lead = db.scalars(select(Lead).where(Lead.phone == "+15557654321")).one()
    assert (lead.name, lead.intent, lead.preferred_channel) == ("Wes", "Hot", "whatsapp")


def test_whatsapp_verify_disabled_without_token(client: TestClient) -> None:
    r = client.get("/api/webhooks/whatsapp", params={"hub.mode": "subscribe",
                                                     "hub.verify_token": "", "hub.challenge": "1"})
    assert r.status_code == 403


# ---------------------------------------------------------- calendar ---


def test_availability_and_slots(client: TestClient, auth: dict[str, str]) -> None:
    av = client.get("/api/calendar/availability", headers=auth).json()
    assert av["timezone"] == "UTC" and len(av["windows"]) == 5
    bad = client.put("/api/calendar/availability", headers=auth,
                     json=[{"weekday": 0, "start_time": "10:00", "end_time": "09:00"}])
    assert bad.status_code == 422
    assert client.put("/api/calendar/availability", headers=auth,
                      json=[{"weekday": 9, "start_time": "10:00",
                             "end_time": "11:00"}]).status_code == 422
    new = client.put("/api/calendar/availability", headers=auth,
                     json=[{"weekday": d, "start_time": "10:00", "end_time": "11:00"}
                           for d in range(7)]).json()
    assert len(new["windows"]) == 7
    day = (utcnow() + timedelta(days=2)).date().isoformat()
    slots = client.get(f"/api/calendar/slots?date={day}", headers=auth).json()["slots"]
    assert [s["starts_at"][11:16] for s in slots] == ["10:00", "10:30"]
    assert slots[0]["label"].endswith("(UTC)")
    assert client.get("/api/calendar/slots?date=2026-02-30", headers=auth).status_code == 422
    assert client.get("/api/calendar/slots?date=tomorrow", headers=auth).status_code == 422


def test_appointments_api(client: TestClient, auth: dict[str, str]) -> None:
    lid = client.post("/api/leads", headers=auth, json={"name": "A", "email": "a@x.example"}
                      ).json()["id"]
    start = (utcnow() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    iso = start.isoformat() + "Z"
    r = client.post("/api/appointments", headers=auth, json={"lead_id": lid, "starts_at": iso})
    assert r.status_code == 201
    appt = r.json()
    assert appt["starts_at"] == iso and appt["status"] == "confirmed"
    assert client.get(f"/api/leads/{lid}", headers=auth).json()["stage"] == "Booked"
    clash = client.post("/api/appointments", headers=auth, json={"lead_id": lid,
                                                                 "starts_at": iso})
    assert clash.status_code == 409
    past = client.post("/api/appointments", headers=auth,
                       json={"lead_id": lid, "starts_at": "2020-01-01T10:00:00+02:00"})
    assert past.status_code == 409
    assert client.post("/api/appointments", headers=auth,
                       json={"lead_id": lid, "starts_at": "soon"}).status_code == 422
    assert client.post("/api/appointments", headers=auth,
                       json={"lead_id": 999, "starts_at": iso}).status_code == 404
    ics = client.get(f"/api/appointments/{appt['id']}/ics", headers=auth)
    assert ics.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in ics.text
    assert client.get("/api/appointments/999/ics", headers=auth).status_code == 404
    cancelled = client.post(f"/api/appointments/{appt['id']}/cancel", headers=auth).json()
    assert cancelled["status"] == "cancelled"
    assert client.get("/api/appointments", headers=auth).json()["appointments"] == []
    assert len(client.get("/api/appointments?upcoming=false",
                          headers=auth).json()["appointments"]) == 1
    assert client.post("/api/appointments/999/cancel", headers=auth).status_code == 404


def test_calendar_feed(client: TestClient, auth: dict[str, str]) -> None:
    rotated = client.post("/api/calendar/feed-token", headers=auth).json()
    feed = client.get(rotated["url"])
    assert feed.status_code == 200 and feed.text.startswith("BEGIN:VCALENDAR")
    newer = client.post("/api/calendar/feed-token", headers=auth).json()
    assert client.get(rotated["url"]).status_code == 404
    assert client.get(newer["url"]).status_code == 200
    assert client.get("/api/calendar/feed/nope.ics").status_code == 404


# --------------------------------------------------------- campaigns ---


def test_campaigns_api(client: TestClient, auth: dict[str, str]) -> None:
    c = client.post("/api/campaigns", headers=auth,
                    json={"name": "Nudge", "template_text": "Hi {first_name}",
                          "followup_hours": 1, "max_followups": 1}).json()
    assert client.post("/api/campaigns", headers=auth,
                       json={"name": "", "template_text": "x"}).status_code == 422
    a = client.post("/api/leads", headers=auth, json={"name": "Ann Lee"}).json()["id"]
    b = client.post("/api/leads", headers=auth, json={"name": "Bo"}).json()["id"]
    client.post(f"/api/leads/{b}/messages", headers=auth, json={"text": "stop"})
    enrolled = client.post(f"/api/campaigns/{c['id']}/enroll", headers=auth,
                           json={"lead_ids": [a, a, b, 9999]}).json()
    assert enrolled == {"enrolled": 1, "skipped": [b, 9999]}
    again = client.post(f"/api/campaigns/{c['id']}/enroll", headers=auth,
                        json={"lead_ids": [a]}).json()
    assert again == {"enrolled": 0, "skipped": [a]}
    listing = client.get("/api/campaigns", headers=auth).json()["campaigns"]
    assert listing[0]["active_enrollments"] == 1
    assert client.post(f"/api/campaigns/{c['id']}/run-now", headers=auth).json()["count"] == 0
    paused = client.patch(f"/api/campaigns/{c['id']}", headers=auth,
                          json={"active": False}).json()
    assert paused["active"] is False
    assert client.patch("/api/campaigns/999", headers=auth, json={}).status_code == 404
    assert client.post("/api/campaigns/999/run-now", headers=auth).status_code == 404


# ----------------------------------------------------- integrations ---


def test_integrations_and_copilot(client: TestClient, auth: dict[str, str]) -> None:
    info = client.get("/api/integrations", headers=auth).json()
    assert info["llm"]["name"] == "offline"
    assert info["crm"]["name"] == "local" and info["calendar"]["name"] == "local"
    assert info["config_problems"] == [] and info["outbox"] == {}
    ch = client.get("/api/channels", headers=auth).json()
    assert ch["outbound_default"] == "simulator" and len(ch["adapters"]) == 3
    ans = client.post("/api/copilot", headers=auth, json={"question": "how many leads?"}).json()
    assert ans["answer"] == "There are 0 leads total."
    assert client.post("/api/copilot", headers=auth, json={"question": ""}).status_code == 422


def test_hubspot_mode_enqueues_outbox(db: Session) -> None:
    with make_client(CRM_PROVIDER="hubspot", HUBSPOT_ACCESS_TOKEN="pat-test") as c:
        tok = c.post("/api/auth/login", json=ADMIN).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        c.post("/api/leads", headers=h, json={"name": "A", "email": "a@x.example"})
        info = c.get("/api/integrations", headers=h).json()
        assert info["crm"]["name"] == "hubspot"
        assert info["outbox"] == {"pending": 1}
    assert db.scalars(select(OutboxJob)).one().kind == "crm.sync_lead"


def test_mock_llm_mode(client: TestClient) -> None:
    with make_client(LLM_PROVIDER="mock") as c:
        tok = c.post("/api/auth/login", json=ADMIN).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        lid = c.post("/api/leads", headers=h, json={"name": "A"}).json()["id"]
        c.post(f"/api/leads/{lid}/messages", headers=h, json={"text": "demo please"})
        lead = c.get(f"/api/leads/{lid}", headers=h).json()
        assert lead["intent_reasons"][0].startswith("llm:")
        assert c.get("/api/integrations", headers=h).json()["llm"]["name"] == "mock"
