"""ClusterX OS product tests: agent, copilot, calendar, scheduler, channels, API."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent as agent_logic
from app import calendar_util, copilot, models
from app.ai.providers import LocalProvider
from app.channels import ConfigurationError, SimulatorAdapter, TwilioAdapter, get_adapter
from app.core.db import SessionLocal
from app.main import app
from app.scheduler import run_due_followups

client = TestClient(app)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fake_lead(**kw):
    base = {"slots_json": "{}", "name": "Test Lead", "stage": "New"}
    base.update(kw)
    return SimpleNamespace(**base)


def _login():
    r = client.post("/api/auth/login", json={
        "email": "admin@clusterx.local", "password": "ChangeMe123!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ------------------------------------------------------------- agent ---

def test_agent_walks_greet_to_book():
    provider = LocalProvider()
    lead = _fake_lead()
    slots, history = {}, []
    replies = []
    convo = [
        "Hi, we are interested in a demo of your product for our sales team",
        "Our budget is $5000",
        "We want this live next month",
        "Yes, send me the proposal",
    ]
    expected_stages = ["budget", "timeline", "intent", "book"]
    for text, expected in zip(convo, expected_stages):
        lead.slots_json = json.dumps(slots)
        result = agent_logic.process_inbound(lead, text, provider, history)
        history.append(text)
        slots.update(result.slot_updates)
        replies.append(result.reply)
        assert result.agent_stage == expected, (text, result.agent_stage)
    assert slots["need"]
    assert slots["budget"] == "$5,000"
    assert slots["timeline"] == "next month"
    label, conf, reasons = result.intent
    assert label == "Hot" and conf > 0
    assert reasons
    assert len(set(replies)) == 4  # reply differs per state


def test_agent_opt_out_short_circuits():
    provider = LocalProvider()
    lead = _fake_lead()
    result = agent_logic.process_inbound(lead, "Not interested, stop messaging me", provider, [])
    assert result.agent_stage == "handoff"
    assert result.intent[0] == "Unqualified"


def test_classify_intent_buckets():
    assert agent_logic.classify_intent(["please send the proposal and pricing"])[0] == "Hot"
    assert agent_logic.classify_intent(["maybe later, just send info"])[0] == "Warm"
    assert agent_logic.classify_intent(["just looking around, not sure"])[0] == "Cold"
    assert agent_logic.classify_intent(["not interested, stop"])[0] == "Unqualified"
    label, conf, _ = agent_logic.classify_intent([])
    assert label == "Cold" and conf == 0.4


def test_extract_budget_variants():
    assert agent_logic.extract_budget("budget is $5000") == "$5,000"
    assert agent_logic.extract_budget("\u20b92.5 lakh") == "\u20b9250,000"
    assert agent_logic.extract_budget("no numbers here") is None


# ------------------------------------------------------------ copilot ---

def test_copilot_hot_leads():
    db = SessionLocal()
    try:
        out = copilot.answer_question(db, "how many hot leads?")
        assert "hot leads" in out["answer"]
        assert int(out["answer"].split()[2]) >= 1
    finally:
        db.close()


def test_copilot_stage_count():
    db = SessionLocal()
    try:
        out = copilot.answer_question(db, "how many leads in Qualified?")
        assert "Qualified" in out["answer"]
    finally:
        db.close()


def test_copilot_last_week_and_no_reply():
    db = SessionLocal()
    try:
        out = copilot.answer_question(db, "show leads from last week")
        assert out["rows"], "seeded leads should appear"
        out = copilot.answer_question(db, "leads with no reply")
        assert "never replied" in out["answer"] and out["rows"]
    finally:
        db.close()


def test_copilot_appointments_and_unknown():
    db = SessionLocal()
    try:
        out = copilot.answer_question(db, "booked appointments this week")
        assert "appointments" in out["answer"]
        out = copilot.answer_question(db, "what is the meaning of life?")
        assert "can't answer" in out["answer"]
    finally:
        db.close()


# ------------------------------------------------------------ calendar ---

def _next_monday_10am():
    now = _now()
    days_ahead = (7 - now.weekday()) % 7 or 7
    # The seed books a demo appointment for tomorrow 10:00; when today is
    # Sunday that is next Monday 10:00, so skip a week to avoid the clash.
    if days_ahead < 2:
        days_ahead += 7
    return (now + timedelta(days=days_ahead)).replace(
        hour=10, minute=0, second=0, microsecond=0)


def test_calendar_slots_booking_conflict_and_ics():
    db = SessionLocal()
    try:
        lead = db.query(models.Lead).filter(
            models.Lead.email == "karan@saasly.com").first()
        start = _next_monday_10am()
        slots = calendar_util.free_slots(start.date(), db)
        assert len(slots) == 16
        appt = calendar_util.book_appointment(
            db, lead.id, "Test call", start, attendee_email=lead.email)
        db.commit()
        slots2 = calendar_util.free_slots(start.date(), db)
        assert len(slots2) == 15
        with pytest.raises(ValueError):
            calendar_util.book_appointment(db, lead.id, "Clash", start)
        ics = calendar_util.ics_for_appointment(appt)
        assert "BEGIN:VCALENDAR" in ics and appt.ics_uid in ics
        feed = calendar_util.feed_ics(db)
        assert feed.count("BEGIN:VEVENT") >= 1
    finally:
        db.close()


# ----------------------------------------------------------- scheduler ---

def test_scheduler_sends_due_followup_and_skips_replied():
    db = SessionLocal()
    try:
        camp = models.Campaign(name="Sched test", template_text="Hi {name}",
                               followup_hours=1, active=True)
        db.add(camp)
        db.flush()
        quiet = models.Lead(name="Quiet One", email="quiet@example.com", source="manual")
        chatty = models.Lead(name="Chatty One", email="chatty@example.com", source="manual")
        db.add_all([quiet, chatty])
        db.flush()
        past = _now() - timedelta(hours=2)
        db.add(models.Enrollment(campaign_id=camp.id, lead_id=quiet.id,
                                 last_agent_msg_at=past, status="active"))
        db.add(models.Enrollment(campaign_id=camp.id, lead_id=chatty.id,
                                 last_agent_msg_at=past, status="active"))
        db.add(models.Message(lead_id=chatty.id, role="agent", text="hello",
                              channel="campaign", created_at=past))
        db.add(models.Message(lead_id=chatty.id, role="lead", text="hi back",
                              channel="simulator",
                              created_at=past + timedelta(minutes=5)))
        db.commit()
        sent = run_due_followups(db, campaign_id=camp.id)
        assert [s["lead_id"] for s in sent] == [quiet.id]
        msg = db.query(models.Message).filter(
            models.Message.lead_id == quiet.id).order_by(
            models.Message.id.desc()).first()
        assert msg.channel == "campaign" and "Quiet One" in msg.text
    finally:
        db.close()


# ------------------------------------------------------------ channels ---

def test_simulator_adapter_writes_message():
    db = SessionLocal()
    try:
        lead = db.query(models.Lead).filter(
            models.Lead.email == "karan@saasly.com").first()
        assert SimulatorAdapter().send(db, lead, "hello from agent") is True
        db.commit()
    finally:
        db.close()


def test_twilio_without_creds_raises_configuration_error():
    db = SessionLocal()
    try:
        lead = db.query(models.Lead).first()
        with pytest.raises(ConfigurationError):
            TwilioAdapter().send(db, lead, "hi")
        with pytest.raises(ConfigurationError):
            get_adapter("nope")
    finally:
        db.close()


# ----------------------------------------------------------------- API ---

def test_api_pages_render():
    headers = _login()
    for path, needle in [
        ("/", "Outbound Dashboard"),
        ("/leads", "Leads"),
        ("/calendar", "Calendar"),
        ("/campaigns", "Campaigns"),
        ("/copilot", "Co-pilot"),
        ("/channels", "Channels"),
    ]:
        r = client.get(path, headers=headers)
        assert r.status_code == 200, path
        assert needle in r.text, path


def test_api_lead_lifecycle_messages_appointment_copilot():
    headers = _login()
    r = client.post("/api/leads", headers=headers, json={
        "name": "API Test", "email": "apitest@example.com",
        "company": "APITestCo", "phone": "+10000000001"})
    assert r.status_code == 201, r.text
    lead_id = r.json()["id"]

    replies = []
    for text in ["We need faster lead follow up, interested in a demo",
                 "Budget is $3000", "Timeline is next month"]:
        r = client.post(f"/api/leads/{lead_id}/messages", headers=headers,
                        json={"text": text})
        assert r.status_code == 200, r.text
        replies.append(r.json()["reply"])
    assert len(set(replies)) == 3
    lead = r.json()["lead"]
    assert lead["stage"] == "Qualified"  # Hot intent auto-advances Contacted -> Qualified
    assert lead["intent"] == "Hot"

    r = client.post("/api/appointments", headers=headers, json={
        "lead_id": lead_id,
        "starts_at": (_next_monday_10am() + timedelta(hours=1)).isoformat()})
    assert r.status_code == 201, r.text
    appt_id = r.json()["id"]
    r = client.get(f"/api/appointments/{appt_id}/ics", headers=headers)
    assert r.status_code == 200 and "BEGIN:VCALENDAR" in r.text

    r = client.post("/api/copilot", headers=headers,
                    json={"question": "how many hot leads?"})
    assert r.status_code == 200 and "hot leads" in r.json()["answer"]

    r = client.post(f"/api/leads/{lead_id}/stage", headers=headers,
                    json={"stage": "Won"})
    assert r.status_code == 200 and r.json()["stage"] == "Won"


def test_api_webhook_auth_and_csv_import():
    headers = _login()
    r = client.post("/api/webhooks/leads", json={"name": "No Key"})
    assert r.status_code == 401
    r = client.post("/api/webhooks/leads", headers={"X-API-Key": "dev-webhook-key"},
                    json={"name": "Webhook Lead", "email": "wh@example.com"})
    assert r.status_code == 201 and r.json()["source"] == "webhook"

    csv_body = "name,email,company\nCsv One,csv1@example.com,CsvCo\nCsv Two,csv2@example.com,CsvCo\n"
    r = client.post("/api/leads/import-csv", headers=headers,
                    files={"file": ("leads.csv", io.BytesIO(csv_body.encode()), "text/csv")})
    assert r.status_code == 200 and r.json()["imported"] == 2


def test_api_campaign_create_enroll_run_now():
    headers = _login()
    r = client.post("/api/campaigns", headers=headers, json={
        "name": "API Campaign", "template_text": "Hi {name}, bumping this.",
        "followup_hours": 24})
    assert r.status_code == 201, r.text
    camp_id = r.json()["id"]
    r = client.post("/api/leads", headers=headers,
                    json={"name": "Camp Lead", "email": "camplead@example.com"})
    lead_id = r.json()["id"]
    r = client.post(f"/api/campaigns/{camp_id}/enroll", headers=headers,
                    json={"lead_ids": [lead_id]})
    assert r.json()["enrolled"] == 1
    # Force due by backdating last_agent_msg_at, then run-now must send.
    db = SessionLocal()
    try:
        enr = db.query(models.Enrollment).filter(
            models.Enrollment.campaign_id == camp_id,
            models.Enrollment.lead_id == lead_id).first()
        enr.last_agent_msg_at = _now() - timedelta(hours=30)
        db.commit()
    finally:
        db.close()
    r = client.post(f"/api/campaigns/{camp_id}/run-now", headers=headers)
    assert r.status_code == 200 and r.json()["count"] == 1


def test_api_calendar_feed_token_gating():
    r = client.get("/api/calendar/feed/bad-token.ics")
    assert r.status_code == 404
    db = SessionLocal()
    try:
        token = db.query(models.FeedToken).first().token
    finally:
        db.close()
    r = client.get(f"/api/calendar/feed/{token}.ics")
    assert r.status_code == 200 and "BEGIN:VCALENDAR" in r.text
