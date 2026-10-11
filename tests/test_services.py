"""Lead service, outbox, follow-ups, idempotency, rate limiting, security, copilot, config."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clusterx_os import copilot, idempotency, security
from clusterx_os.config import DEFAULT_ADMIN_PASSWORD, Settings
from clusterx_os.integrations import ConfigurationError, IntegrationError
from clusterx_os.integrations.crm import LeadSnapshot
from clusterx_os.models import (
    Appointment,
    Campaign,
    Enrollment,
    Event,
    IdempotencyRecord,
    Lead,
    Message,
    OutboxJob,
    utcnow,
)
from clusterx_os.ratelimit import RateLimiter
from clusterx_os.services import followups, leads, outbox


class FakeCRM:
    name = "fake"

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = errors or []
        self.synced: list[LeadSnapshot] = []
        self.notes: list[tuple[str, str]] = []

    def sync_lead(self, lead: LeadSnapshot) -> str:
        if self.errors:
            raise self.errors.pop(0)
        self.synced.append(lead)
        return f"hs-{lead.id}"

    def add_note(self, external_id: str, body: str) -> str:
        self.notes.append((external_id, body))
        return "note-1"

    def status(self) -> dict[str, Any]:
        return {"name": self.name}


class FakeChannel:
    def __init__(self, name: str, fail: Exception | None = None) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, text: str) -> str | None:
        if self.fail:
            raise self.fail
        self.sent.append((to, text))
        return f"{self.name}-{len(self.sent)}"

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": True}


def hubspot(settings: Settings) -> Settings:
    return settings.model_copy(update={"CRM_PROVIDER": "hubspot", "HUBSPOT_ACCESS_TOKEN": "t"})


def jobs(db: Session) -> list[OutboxJob]:
    return list(db.scalars(select(OutboxJob).order_by(OutboxJob.id)))


def events(db: Session, lead_id: int) -> list[str]:
    return [e.kind for e in db.scalars(select(Event).where(Event.lead_id == lead_id)
                                       .order_by(Event.id))]


# -------------------------------------------------------------- leads ---

@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+1 (555) 000-1111", "+15550001111"), ("whatsapp:+639171234567", "+639171234567"),
     ("555.000.1111", "5550001111"), ("", ""), ("  ", ""), ("1234567", "1234567"),
     ("+123456789012345", "+123456789012345")],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert leads.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["123456", "+1234567890123456", "abc1234567", "+", "12-ab-34"])
def test_normalize_phone_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="Invalid phone"):
        leads.normalize_phone(raw)


def test_create_lead_and_crm_enqueue(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, settings, {"name": " Ann ", "email": " ANN@X.example ",
                                            "phone": "+1 555 000 1111", "company": " Acme "},
                             "manual")
    db.commit()
    assert (lead.name, lead.email, lead.phone, lead.company, lead.stage, lead.source) == (
        "Ann", "ann@x.example", "+15550001111", "Acme", "New", "manual")
    assert events(db, lead.id) == ["lead_created"]
    assert jobs(db) == []  # local CRM: nothing to sync
    other = leads.create_lead(db, hubspot(settings), {"email": "b@x.example"}, "api")
    db.commit()
    assert [(j.kind, json.loads(j.payload_json)) for j in jobs(db)] == [
        ("crm.sync_lead", {"lead_id": other.id})]


def test_create_lead_requires_identity(db: Session, settings: Settings) -> None:
    with pytest.raises(ValueError, match="at least"):
        leads.create_lead(db, settings, {"company": "Acme"}, "manual")


def test_upsert_matches_email_then_phone(db: Session, settings: Settings) -> None:
    s = hubspot(settings)
    a, created = leads.upsert_lead(db, s, {"email": "a@x.example", "name": ""}, "webhook")
    assert created
    db.commit()
    db.query(OutboxJob).delete()
    same, created = leads.upsert_lead(db, s, {"email": "A@X.example", "name": "Ann",
                                              "phone": "+15550001111", "company": "Acme"},
                                      "webhook")
    assert not created and same.id == a.id
    assert (same.name, same.phone, same.company) == ("Ann", "+15550001111", "Acme")
    db.commit()
    assert len(jobs(db)) == 1
    by_phone, created = leads.upsert_lead(db, s, {"phone": "+1 555 000 1111", "name": "Other"},
                                          "twilio")
    assert not created and by_phone.id == a.id
    assert by_phone.name == "Ann"  # existing values are never overwritten
    db.commit()
    assert len(jobs(db)) == 1  # nothing changed -> no new sync (and pending one deduped)
    detail = json.loads(db.scalars(select(Event).where(Event.kind == "lead_updated")
                                   .order_by(Event.id)).all()[0].detail_json)
    assert detail == {"source": "webhook", "fields": ["name", "phone", "company"]}


def test_set_stage(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, settings, {"name": "A"}, "manual")
    assert leads.set_stage(db, settings, lead, "Won", by="me") is True
    assert leads.set_stage(db, settings, lead, "Won", by="me") is False
    db.commit()
    stage_events = db.scalars(select(Event).where(Event.kind == "stage_changed")).all()
    assert [json.loads(e.detail_json) for e in stage_events] == [
        {"from": "New", "to": "Won", "by": "me"}]


def test_serialize_lead(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, settings, {"name": "A", "email": "a@x.example"}, "manual")
    lead.slots_json = json.dumps({"need": "n", "_agent_stage": "budget", "intent_asked": True,
                                  "budget": "$1"})
    db.commit()
    data = leads.serialize_lead(lead)
    assert data["slots"] == {"need": "n", "budget": "$1"}
    assert data["agent_stage"] == "budget"
    assert data["created_at"].endswith("Z")
    assert data["preferred_channel"] is None
    assert data["intent_reasons"] == []


def test_deliver_channels(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, settings, {"name": "A", "phone": "+15550001111"}, "manual")
    tw = FakeChannel("twilio")
    adapters: dict[str, Any] = {"twilio": tw}
    m1 = leads.deliver(db, settings, lead, "hi", "twilio", adapters)
    assert (m1.delivery_status, m1.external_id, m1.role) == ("sent", "twilio-1", "agent")
    assert tw.sent == [("+15550001111", "hi")]
    m2 = leads.deliver(db, settings, lead, "hi", "simulator")
    assert (m2.delivery_status, m2.external_id) == ("sent", None)
    m3 = leads.deliver(db, settings, lead, "hi", "api")
    assert m3.delivery_status == "recorded"
    bad = {"whatsapp": FakeChannel("whatsapp", IntegrationError("nope"))}
    m4 = leads.deliver(db, settings, lead, "hi", "whatsapp", bad)  # type: ignore[arg-type]
    assert (m4.delivery_status, m4.error) == ("failed", "nope")
    m5 = leads.deliver(db, settings, lead, "hi", "twilio")  # real adapter, no credentials
    assert m5.delivery_status == "failed" and "TWILIO_ACCOUNT_SID" in m5.error
    db.commit()
    assert events(db, lead.id).count("delivery_failed") == 2


def test_handle_inbound_flow_and_booking(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, hubspot(settings), {"name": "Ann Lee", "email": "a@x.example"},
                             "manual")
    db.commit()
    now = datetime(2026, 10, 14, 8, 0)  # Wednesday
    s = hubspot(settings)
    for text in ["We need a CRM for 5 reps", "around $3k", "next month", "we use sheets"]:
        leads.handle_inbound(db, s, lead, text, now=now)
    out = leads.handle_inbound(db, s, lead, "yes please book", now=now)
    assert "Wed 14 Oct, 9:00 AM (UTC)" in out.reply
    assert out.agent_stage == "book"
    out = leads.handle_inbound(db, s, lead, "Wednesday 2pm", now=now)
    # "Wednesday" means next week's Wednesday
    assert out.appointment is not None
    assert out.appointment.starts_at == datetime(2026, 10, 21, 14, 0)
    assert out.reply == ("You're booked Ann: intro call on Wed 21 Oct, 2:00 PM (UTC). "
                         "A calendar invite is on its way. Reply here if you need to change it.")
    db.refresh(lead)
    assert lead.stage == "Booked"
    msgs = db.scalars(select(Message).where(Message.lead_id == lead.id)).all()
    assert [m.role for m in msgs].count("lead") == 6
    assert [m.role for m in msgs].count("agent") == 6
    kinds = [j.kind for j in jobs(db)]
    assert "crm.note" in kinds and "crm.sync_lead" in kinds
    notes = [json.loads(j.payload_json)["body"] for j in jobs(db) if j.kind == "crm.note"]
    assert "Intro call booked by ClusterX OS for Wed 21 Oct, 2:00 PM (UTC)." in notes


def test_handle_inbound_unavailable_time(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, settings, {"name": "Ann", "email": "a@x.example"}, "manual")
    lead.slots_json = json.dumps({"need": "n", "budget": "$1", "timeline": "q1",
                                  "intent_asked": True, "_agent_stage": "book"})
    db.commit()
    now = datetime(2026, 10, 14, 8, 0)
    out = leads.handle_inbound(db, settings, lead, "saturday 10am", now=now)
    assert out.appointment is None
    assert out.reply.startswith("Sorry Ann, Sat 17 Oct, 10:00 AM (UTC) is not available.")
    assert "Mon 19 Oct, 9:00 AM (UTC)" in out.reply
    assert "booking_unavailable" in events(db, lead.id)
    taken = leads.handle_inbound(db, settings, lead, "monday 9am", now=now)
    assert taken.appointment is not None
    other = leads.create_lead(db, settings, {"name": "Bo", "email": "b@x.example"}, "manual")
    other.slots_json = lead.slots_json
    db.commit()
    clash = leads.handle_inbound(db, settings, other, "monday 9am", now=now)
    assert clash.appointment is None
    assert "Mon 19 Oct, 9:30 AM (UTC)" in clash.reply


def test_handle_inbound_dedupes_external_id(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, settings, {"name": "A", "phone": "+15550001111"}, "twilio")
    db.commit()
    adapters: dict[str, Any] = {"twilio": FakeChannel("twilio")}
    first = leads.handle_inbound(db, settings, lead, "hello", "twilio", "SM1", adapters=adapters)
    assert first.duplicate is False
    again = leads.handle_inbound(db, settings, lead, "hello", "twilio", "SM1", adapters=adapters)
    assert again.duplicate is True and again.reply == ""
    assert lead.preferred_channel == "twilio"
    assert len(db.scalars(select(Message).where(Message.role == "lead")).all()) == 1


def test_handle_inbound_rejects_empty(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, settings, {"name": "A"}, "manual")
    with pytest.raises(ValueError, match="Empty"):
        leads.handle_inbound(db, settings, lead, "   ")


def test_handle_inbound_opt_out_and_back(db: Session, settings: Settings) -> None:
    lead = leads.create_lead(db, hubspot(settings), {"name": "A", "email": "a@x.example"},
                             "manual")
    db.commit()
    leads.handle_inbound(db, hubspot(settings), lead, "stop")
    assert lead.opted_out is True
    assert lead.intent == "Unqualified"
    note = [json.loads(j.payload_json)["body"] for j in jobs(db) if j.kind == "crm.note"]
    assert note == ["ClusterX OS classified intent as Unqualified: explicit opt-out language"]
    leads.handle_inbound(db, settings, lead, "actually we need help")
    assert lead.opted_out is False


def test_handle_inbound_upstream_calendar_failure(db: Session, settings: Settings) -> None:
    from tests.test_booking import FakeCalendar

    lead = leads.create_lead(db, settings, {"name": "Ann", "email": "a@x.example"}, "manual")
    lead.slots_json = json.dumps({"need": "n", "budget": "$1", "timeline": "q1",
                                  "intent_asked": True, "_agent_stage": "book"})
    db.commit()
    out = leads.handle_inbound(db, settings, lead, "monday 9am",
                               calendar=FakeCalendar(fail_create=True),  # type: ignore[arg-type]
                               now=datetime(2026, 10, 14, 8, 0))
    assert out.appointment is None
    assert "not available" in out.reply
    assert db.scalars(select(Appointment)).all() == []


def test_alternatives_tolerate_calendar_outage(db: Session, settings: Settings) -> None:
    class Down:
        name = "down"

        def filter_available(self, c: Any) -> Any:
            raise IntegrationError("down", retryable=True)

    assert leads._alternatives(db, settings, Down(), utcnow(), None) == []  # type: ignore[arg-type]


# ------------------------------------------------------------- outbox ---

def test_backoff() -> None:
    assert [outbox.backoff_seconds(n) for n in (0, 1, 2, 3, 7, 8, 30)] == [
        0, 30, 60, 120, 1920, 3600, 3600]


def test_outbox_dedupes_pending_sync(db: Session, settings: Settings) -> None:
    s = hubspot(settings)
    lead = leads.create_lead(db, s, {"name": "A", "email": "a@x.example"}, "manual")
    assert outbox.enqueue_crm_sync(db, s, lead) is False
    other = Lead(name="B")
    db.add(other)
    db.flush()
    assert outbox.enqueue_crm_sync(db, s, other) is True
    assert outbox.enqueue_crm_note(db, s, lead, "  ") is False
    assert outbox.enqueue_crm_note(db, settings, lead, "x") is False
    assert outbox.enqueue_crm_note(db, s, lead, "x") is True
    db.commit()
    assert [j.kind for j in jobs(db)] == ["crm.sync_lead", "crm.sync_lead", "crm.note"]


def test_process_outbox_success(db: Session, settings: Settings) -> None:
    s = hubspot(settings)
    lead = leads.create_lead(db, s, {"name": "A", "email": "a@x.example"}, "manual")
    outbox.enqueue_crm_note(db, s, lead, "hello")
    db.commit()
    crm = FakeCRM()
    now = utcnow()
    assert outbox.process_outbox(db, s, crm, now=now) == {"done": 2, "retry": 0, "failed": 0}
    db.refresh(lead)
    assert lead.crm_external_id == f"hs-{lead.id}"
    assert lead.crm_synced_at == now
    assert crm.notes == [(f"hs-{lead.id}", "hello")]
    assert all(j.status == "done" and j.attempts == 1 for j in jobs(db))
    assert outbox.process_outbox(db, s, crm) == {"done": 0, "retry": 0, "failed": 0}


def test_process_outbox_note_syncs_first(db: Session, settings: Settings) -> None:
    s = hubspot(settings)
    lead = Lead(name="A", email="a@x.example")
    db.add(lead)
    db.flush()
    outbox.enqueue_crm_note(db, s, lead, "n")
    db.commit()
    crm = FakeCRM()
    outbox.process_outbox(db, s, crm)
    assert len(crm.synced) == 1 and crm.notes == [(f"hs-{lead.id}", "n")]


def test_process_outbox_retry_then_fail(db: Session, settings: Settings) -> None:
    s = hubspot(settings).model_copy(update={"OUTBOX_MAX_ATTEMPTS": 2})
    lead = leads.create_lead(db, s, {"name": "A", "email": "a@x.example"}, "manual")
    db.commit()
    crm = FakeCRM([IntegrationError("503", retryable=True), IntegrationError("503", retryable=True)])
    now = utcnow()
    assert outbox.process_outbox(db, s, crm, now=now) == {"done": 0, "retry": 1, "failed": 0}
    job = jobs(db)[0]
    assert (job.status, job.attempts, job.last_error) == ("pending", 1, "503")
    assert job.next_attempt_at == now + timedelta(seconds=30)
    assert outbox.process_outbox(db, s, crm, now=now) == {"done": 0, "retry": 0, "failed": 0}
    later = now + timedelta(seconds=30)
    assert outbox.process_outbox(db, s, crm, now=later) == {"done": 0, "retry": 0, "failed": 1}
    db.refresh(job)
    assert (job.status, job.attempts) == ("failed", 2)
    assert lead.crm_external_id is None


def test_process_outbox_permanent_and_unexpected_errors(db: Session, settings: Settings) -> None:
    s = hubspot(settings)
    a = leads.create_lead(db, s, {"name": "A", "email": "a@x.example"}, "manual")
    b = leads.create_lead(db, s, {"name": "B", "email": "b@x.example"}, "manual")
    c = leads.create_lead(db, s, {"name": "C", "email": "c@x.example"}, "manual")
    db.commit()
    crm = FakeCRM([IntegrationError("400", retryable=False), RuntimeError("bug"),
                   ConfigurationError("cfg")])
    assert outbox.process_outbox(db, s, crm) == {"done": 0, "retry": 1, "failed": 2}
    statuses = {json.loads(j.payload_json)["lead_id"]: j.status for j in jobs(db)}
    assert statuses == {a.id: "failed", b.id: "pending", c.id: "failed"}


def test_process_outbox_unknown_kind_and_deleted_lead(db: Session, settings: Settings) -> None:
    s = hubspot(settings)
    lead = Lead(name="A")
    db.add(lead)
    db.flush()
    outbox.enqueue(db, "crm.mystery", {"lead_id": lead.id})
    outbox.enqueue(db, outbox.CRM_SYNC, {"lead_id": 999999})
    db.commit()
    assert outbox.process_outbox(db, s, FakeCRM()) == {"done": 1, "retry": 0, "failed": 1}
    assert [j.last_error for j in jobs(db)] == ["Unknown outbox job kind: crm.mystery", ""]


def test_process_outbox_without_crm_and_limit(db: Session, settings: Settings) -> None:
    s = hubspot(settings)
    for i in range(3):
        leads.create_lead(db, s, {"name": f"L{i}", "email": f"{i}@x.example"}, "manual")
    db.commit()
    assert outbox.process_outbox(db, s, None) == {"done": 0, "retry": 0, "failed": 0}
    assert outbox.process_outbox(db, s, FakeCRM(), limit=2)["done"] == 2
    assert outbox.process_outbox(db, s, FakeCRM(), limit=2)["done"] == 1


# ---------------------------------------------------------- follow-ups ---

def _campaign(db: Session, **kw: Any) -> Campaign:
    c = Campaign(name="C", template_text=kw.get("template", "Hi {first_name} at {company}"),
                 followup_hours=kw.get("hours", 24), max_followups=kw.get("max", 2),
                 active=kw.get("active", True))
    db.add(c)
    db.flush()
    return c


def _enroll(db: Session, c: Campaign, lead: Lead, last: datetime | None) -> Enrollment:
    e = Enrollment(campaign_id=c.id, lead_id=lead.id, last_agent_msg_at=last, status="active",
                   followups_sent=0)
    db.add(e)
    db.commit()
    return e


def test_render_template() -> None:
    lead = Lead(name="Ann Lee", company="Acme")
    assert followups.render_template("{first_name}|{name}|{company}", lead) == "Ann|Ann Lee|Acme"
    assert followups.render_template("{first_name}|{name}|{company}", Lead(name="", company="")) \
        == "there|there|your company"


def test_followup_due_and_not_due(db: Session, settings: Settings) -> None:
    now = datetime(2026, 10, 14, 12, 0)
    c = _campaign(db)
    due = Lead(name="Ann Lee", company="Acme")
    fresh = Lead(name="Bo")
    never = Lead(name="Cy")
    db.add_all([due, fresh, never])
    db.flush()
    _enroll(db, c, due, now - timedelta(hours=24))
    _enroll(db, c, fresh, now - timedelta(hours=23, minutes=59))
    _enroll(db, c, never, None)
    sent = followups.run_due_followups(db, settings, now=now)
    assert sorted(x["lead_id"] for x in sent) == sorted([due.id, never.id])
    msg = db.scalars(select(Message).where(Message.lead_id == due.id)).one()
    assert msg.text == "Hi Ann at Acme" and msg.channel == "simulator"
    enr = db.scalars(select(Enrollment).where(Enrollment.lead_id == due.id)).one()
    assert (enr.followups_sent, enr.last_agent_msg_at) == (1, now)


def test_followup_skips_when_lead_replied(db: Session, settings: Settings) -> None:
    now = datetime(2026, 10, 14, 12, 0)
    c = _campaign(db)
    lead = Lead(name="A")
    db.add(lead)
    db.flush()
    last = now - timedelta(hours=30)
    enr = _enroll(db, c, lead, last)
    db.add(Message(lead_id=lead.id, role="lead", text="hi", created_at=last + timedelta(hours=1)))
    db.commit()
    assert followups.run_due_followups(db, settings, now=now) == []
    db.refresh(enr)
    assert enr.last_agent_msg_at == now and enr.followups_sent == 0


def test_followup_stop_conditions(db: Session, settings: Settings) -> None:
    now = datetime(2026, 10, 14, 12, 0)
    c = _campaign(db, max=1)
    opted = Lead(name="A", opted_out=True)
    unq = Lead(name="B", intent="Unqualified")
    booked = Lead(name="C", stage="Booked")
    maxed = Lead(name="D")
    db.add_all([opted, unq, booked, maxed])
    db.flush()
    for lead in (opted, unq, booked):
        _enroll(db, c, lead, None)
    e = _enroll(db, c, maxed, None)
    e.followups_sent = 1
    db.commit()
    assert followups.run_due_followups(db, settings, now=now) == []
    statuses = {en.lead_id: en.status for en in db.scalars(select(Enrollment))}
    assert statuses == {opted.id: "stopped", unq.id: "stopped", booked.id: "stopped",
                        maxed.id: "completed"}
    reasons = sorted(json.loads(ev.detail_json)["reason"]
                     for ev in db.scalars(select(Event).where(Event.kind == "campaign_ended")))
    assert reasons == ["max_followups", "opted_out", "opted_out", "stage_booked"]


def test_followup_inactive_campaign_and_filter(db: Session, settings: Settings) -> None:
    now = datetime(2026, 10, 14, 12, 0)
    off = _campaign(db, active=False)
    on = _campaign(db)
    lead = Lead(name="A")
    db.add(lead)
    db.flush()
    _enroll(db, off, lead, None)
    _enroll(db, on, lead, None)
    assert followups.run_due_followups(db, settings, now=now, campaign_id=off.id) == []
    sent = followups.run_due_followups(db, settings, now=now, campaign_id=on.id)
    assert sent == [{"lead_id": lead.id, "campaign_id": on.id, "channel": "simulator"}]


def test_followup_delivery_failure_retries(db: Session, settings: Settings) -> None:
    now = datetime(2026, 10, 14, 12, 0)
    c = _campaign(db)
    lead = Lead(name="A", phone="+15550001111", preferred_channel="twilio")
    db.add(lead)
    db.flush()
    enr = _enroll(db, c, lead, None)
    bad: dict[str, Any] = {"twilio": FakeChannel("twilio", IntegrationError("down"))}
    assert followups.run_due_followups(db, settings, now=now, adapters=bad) == []
    db.refresh(enr)
    assert enr.followups_sent == 0 and enr.last_agent_msg_at is None
    good: dict[str, Any] = {"twilio": FakeChannel("twilio")}
    sent = followups.run_due_followups(db, settings, now=now, adapters=good)
    assert sent[0]["channel"] == "twilio"


def test_followup_missing_lead(db: Session, settings: Settings) -> None:
    c = _campaign(db)
    lead = Lead(name="A")
    db.add(lead)
    db.flush()
    enr = _enroll(db, c, lead, None)
    real_get = db.get

    def get(model: Any, ident: Any) -> Any:
        return None if model is Lead else real_get(model, ident)

    db.get = get  # type: ignore[method-assign]
    assert followups.run_due_followups(db, settings) == []
    db.refresh(enr)
    assert enr.status == "stopped"


# --------------------------------------------------------- idempotency ---

def test_idempotency_roundtrip(db: Session) -> None:
    fp = idempotency.request_fingerprint({"b": 1, "a": [1, 2]})
    assert fp == idempotency.request_fingerprint({"a": [1, 2], "b": 1})
    assert fp != idempotency.request_fingerprint({"a": [2, 1], "b": 1})
    assert idempotency.lookup(db, "s", "k", fp) is None
    assert idempotency.store(db, "s", "k", fp, 201, {"id": 1}) is True
    stored = idempotency.lookup(db, "s", "k", fp)
    assert stored == idempotency.StoredResponse(201, {"id": 1})
    assert idempotency.lookup(db, "other", "k", fp) is None
    with pytest.raises(idempotency.IdempotencyConflictError):
        idempotency.lookup(db, "s", "k", "different")
    assert idempotency.store(db, "s", "k", fp, 201, {"id": 2}) is False


def test_idempotency_key_validation() -> None:
    assert idempotency.validate_key("  abc ") == "abc"
    assert idempotency.validate_key("x" * 255) == "x" * 255
    for bad in ("", "   ", "x" * 256):
        with pytest.raises(ValueError):
            idempotency.validate_key(bad)


def test_idempotency_purge(db: Session) -> None:
    now = utcnow()
    db.add(IdempotencyRecord(scope="s", key="old", request_hash="h", status_code=200,
                             response_json="{}", created_at=now - timedelta(hours=25)))
    db.add(IdempotencyRecord(scope="s", key="new", request_hash="h", status_code=200,
                             response_json="{}", created_at=now - timedelta(hours=23)))
    db.commit()
    assert idempotency.purge_expired(db, 24, now=now) == 1
    assert [r.key for r in db.scalars(select(IdempotencyRecord))] == ["new"]
    assert idempotency.purge_expired(db, 24, now=now) == 0


# ---------------------------------------------------------- rate limit ---

def test_rate_limiter_window() -> None:
    t = [100.0]
    rl = RateLimiter(clock=lambda: t[0])
    assert rl.hit("k", 2) == (True, 0)
    assert rl.hit("k", 2) == (True, 0)
    assert rl.hit("k", 2) == (False, 61)
    assert rl.hit("other", 2) == (True, 0)
    t[0] = 159.5
    assert rl.hit("k", 2) == (False, 1)
    t[0] = 160.0  # first hit falls out of the window exactly now
    assert rl.hit("k", 2) == (True, 0)
    rl.reset()
    assert rl.hit("k", 1) == (True, 0)
    with pytest.raises(ValueError):
        rl.hit("k", 0)


# ------------------------------------------------------------ security ---

def test_passwords() -> None:
    h = security.hash_password("correct horse 1")
    assert h.startswith("$2b$04$")
    assert security.verify_password("correct horse 1", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("x", "not-a-hash")
    long_a, long_b = "a" * 80 + "1", "a" * 80 + "2"
    assert not security.verify_password(long_b, security.hash_password(long_a))


@pytest.mark.parametrize("pw", ["short1", "onlyletterslong", "1234567890"])
def test_password_strength_rejects(pw: str) -> None:
    with pytest.raises(ValueError):
        security.validate_password_strength(pw)


def test_password_strength_accepts() -> None:
    security.validate_password_strength("abcdefghi1")
    security.validate_password_strength(DEFAULT_ADMIN_PASSWORD)


def test_tokens() -> None:
    tok = security.create_access_token("a@x.example")
    assert security.decode_token(tok) == "a@x.example"
    assert security.decode_token(security.create_access_token("a@x.example", -1)) is None
    assert security.decode_token("garbage") is None
    assert security.decode_token(security.create_access_token("")) is None
    assert security.constant_time_equals("abc", "abc")
    assert not security.constant_time_equals("abc", "abd")


# ------------------------------------------------------------- copilot ---

def test_copilot_answers(db: Session) -> None:
    now = datetime(2026, 10, 14, 12, 0)  # Wednesday
    db.add_all([
        Lead(name="H1", intent="Hot", stage="Qualified", created_at=now - timedelta(hours=1)),
        Lead(name="H2", intent="Hot", stage="Booked", created_at=now - timedelta(days=1)),
        Lead(name="W", intent="Warm", stage="Contacted", created_at=now - timedelta(days=9)),
        Lead(name="N", stage="Won", created_at=now - timedelta(days=3)),
    ])
    db.commit()
    first = db.scalars(select(Lead).where(Lead.name == "H1")).one()
    db.add(Message(lead_id=first.id, role="lead", text="hi"))
    db.add(Appointment(lead_id=first.id, title="Call", starts_at=now + timedelta(days=1),
                       ends_at=now + timedelta(days=1, minutes=30), ics_uid="u1"))
    db.add(Appointment(lead_id=first.id, title="Later", starts_at=now + timedelta(days=9),
                       ends_at=now + timedelta(days=9, minutes=30), ics_uid="u2"))
    db.commit()

    def ask(q: str) -> dict[str, Any]:
        return copilot.answer_question(db, q, now=now)

    assert ask("How many hot leads?")["answer"] == "There are 2 hot leads."
    assert ask("how many qualified leads")["answer"] == "There are 1 leads in stage 'Qualified'."
    assert ask("how many leads in booked")["answer"] == "There are 1 leads in stage 'Booked'."
    assert ask("how many leads?")["answer"] == (
        "There are 4 leads total. By stage: Contacted: 1, Qualified: 1, Booked: 1, Won: 1.")
    assert [r["name"] for r in ask("show leads from today")["rows"]] == ["H1"]
    assert [r["name"] for r in ask("show leads from yesterday")["rows"]] == ["H2"]
    assert [r["name"] for r in ask("list leads from this week")["rows"]] == ["H1", "H2"]
    assert [r["name"] for r in ask("show leads from last week")["rows"]] == ["N", "W"]
    assert [r["name"] for r in ask("show leads from last 7 days")["rows"]] == ["H1", "H2", "N"]
    assert sorted(r["name"] for r in ask("leads with no reply")["rows"]) == ["H2", "N", "W"]
    up = ask("upcoming appointments")
    assert up["answer"] == "1 appointments in the next 7 days."
    assert up["rows"][0]["title"] == "Call"
    assert ask("appointments")["answer"] == "There are 2 confirmed appointments."
    assert ask("intent breakdown")["answer"] == (
        "Leads by intent: Hot: 2, Warm: 1, Cold: 0, Unqualified: 0.")
    assert ask("conversion rate")["answer"] == "2/4 leads booked or won (50%); 1 won (25%)."
    assert [r["name"] for r in ask("show me warm leads")["rows"]] == ["W"]
    assert ask("what is the meaning of life")["answer"] == copilot.HELP
    assert ask("how many purple leads")["answer"] == copilot.HELP


def test_copilot_empty_db(db: Session) -> None:
    assert copilot.answer_question(db, "conversion rate")["answer"] == (
        "No leads yet, so no conversion rate.")
    assert copilot.answer_question(db, "how many leads")["answer"] == "There are 0 leads total."


def test_copilot_windows() -> None:
    now = datetime(2026, 10, 14, 12, 0)
    assert copilot.window("today", now)[0] == datetime(2026, 10, 14)
    assert copilot.window("yesterday", now) == (datetime(2026, 10, 13), datetime(2026, 10, 14))
    assert copilot.window("this week", now)[0] == datetime(2026, 10, 12)
    assert copilot.window("last week", now) == (datetime(2026, 10, 5), datetime(2026, 10, 12))
    assert copilot.window("last 7 days", now)[0] == datetime(2026, 10, 7, 12, 0)


# -------------------------------------------------------------- config ---

def test_config_problems(settings: Settings) -> None:
    assert settings.problems() == []
    prod = settings.model_copy(update={"APP_ENV": "production",
                                       "SECRET_KEY": "dev-insecure-secret-key-change-me-in-production",
                                       "WEBHOOK_API_KEY": "short",
                                       "ADMIN_PASSWORD": DEFAULT_ADMIN_PASSWORD,
                                       "SEED_DEMO_DATA": True})
    assert len(prod.problems()) == 4
    ok = settings.model_copy(update={"APP_ENV": "production", "SECRET_KEY": "s" * 32,
                                     "WEBHOOK_API_KEY": "w" * 16, "ADMIN_PASSWORD": "Strong-pass-1"})
    assert ok.problems() == []
    assert ok.model_copy(update={"SECRET_KEY": "s" * 31}).problems() != []
    assert ok.model_copy(update={"WEBHOOK_API_KEY": "w" * 15}).problems() != []
    integ = settings.model_copy(update={"LLM_PROVIDER": "openai", "CRM_PROVIDER": "hubspot",
                                        "CALENDAR_PROVIDER": "google"})
    assert len(integ.problems()) == 3
    cal = settings.model_copy(update={"CALENDAR_PROVIDER": "calcom", "CALCOM_API_KEY": "k"})
    assert cal.problems() == [
        "CALENDAR_PROVIDER=calcom requires CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID"]


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Settings(BUSINESS_TIMEZONE="Mars/Base")
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings(LOG_LEVEL="loud")
    s = Settings(LOG_LEVEL="debug", CORS_ORIGINS=" https://a.example , ,https://b.example")
    assert s.LOG_LEVEL == "DEBUG"
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert Settings(BUSINESS_TIMEZONE="Asia/Manila").tz.key == "Asia/Manila"
