"""Lead lifecycle: intake, conversation handling, delivery, CRM hand-off."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clusterx_os import agent, booking
from clusterx_os.config import Settings
from clusterx_os.integrations import ConfigurationError, IntegrationError
from clusterx_os.integrations.calendars import CalendarAdapter
from clusterx_os.integrations.channels import ChannelAdapter, build_adapters
from clusterx_os.integrations.llm import LLMClient
from clusterx_os.models import Appointment, Event, Lead, Message, utcnow
from clusterx_os.services import outbox

logger = logging.getLogger(__name__)

_PHONE_STRIP_RE = re.compile(r"[\s\-().]")
EXTERNAL_CHANNELS = ("twilio", "whatsapp")
LEAD_FIELDS = ("name", "email", "phone", "company")


def normalize_phone(raw: str) -> str:
    """Strip formatting; keep a leading '+'. Raises ValueError for junk."""
    value = raw.strip()
    if value.startswith("whatsapp:"):
        value = value[len("whatsapp:"):]
    if not value:
        return ""
    value = _PHONE_STRIP_RE.sub("", value)
    plus = value.startswith("+")
    digits = value[1:] if plus else value
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        raise ValueError(f"Invalid phone number: {raw!r}")
    return ("+" if plus else "") + digits


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def log_event(db: Session, lead_id: int, kind: str, detail: dict[str, Any] | None = None) -> None:
    db.add(Event(lead_id=lead_id, kind=kind, detail_json=json.dumps(detail or {}, default=str)))


def serialize_lead(lead: Lead) -> dict[str, Any]:
    slots = json.loads(lead.slots_json or "{}")
    return {
        "id": lead.id,
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "company": lead.company,
        "source": lead.source,
        "stage": lead.stage,
        "intent": lead.intent,
        "intent_confidence": lead.intent_confidence,
        "intent_reasons": json.loads(lead.intent_reasons_json or "[]"),
        "slots": {k: v for k, v in slots.items() if k in ("need", "budget", "timeline")},
        "agent_stage": slots.get("_agent_stage", "greet"),
        "opted_out": lead.opted_out,
        "preferred_channel": lead.preferred_channel or None,
        "crm_external_id": lead.crm_external_id,
        "created_at": lead.created_at.isoformat() + "Z" if lead.created_at else None,
    }


def find_lead(db: Session, email: str = "", phone: str = "") -> Lead | None:
    if email:
        lead = db.scalar(select(Lead).where(Lead.email == email).order_by(Lead.id).limit(1))
        if lead is not None:
            return lead
    if phone:
        return db.scalar(select(Lead).where(Lead.phone == phone).order_by(Lead.id).limit(1))
    return None


def create_lead(db: Session, settings: Settings, data: dict[str, str], source: str,
                event_kind: str = "lead_created") -> Lead:
    lead = Lead(
        name=data.get("name", "").strip(),
        email=normalize_email(data.get("email", "")),
        phone=normalize_phone(data.get("phone", "")),
        company=data.get("company", "").strip(),
        source=source,
        stage="New",
    )
    if not (lead.name or lead.email or lead.phone):
        raise ValueError("A lead needs at least a name, email or phone")
    db.add(lead)
    db.flush()
    log_event(db, lead.id, event_kind, {"source": source})
    outbox.enqueue_crm_sync(db, settings, lead)
    return lead


def upsert_lead(db: Session, settings: Settings, data: dict[str, str], source: str,
                event_kind: str = "lead_created") -> tuple[Lead, bool]:
    """Match on email, then phone; fill blanks on a match. Returns (lead, created)."""
    email = normalize_email(data.get("email", ""))
    phone = normalize_phone(data.get("phone", ""))
    existing = find_lead(db, email, phone)
    if existing is None:
        return create_lead(db, settings, data, source, event_kind), True
    incoming = {"name": data.get("name", "").strip(), "email": email, "phone": phone,
                "company": data.get("company", "").strip()}
    changed = []
    for key in LEAD_FIELDS:
        if incoming[key] and not getattr(existing, key):
            setattr(existing, key, incoming[key])
            changed.append(key)
    log_event(db, existing.id, "lead_updated", {"source": source, "fields": changed})
    if changed:
        outbox.enqueue_crm_sync(db, settings, existing)
    return existing, False


def set_stage(db: Session, settings: Settings, lead: Lead, stage: str, by: str) -> bool:
    if stage == lead.stage:
        return False
    log_event(db, lead.id, "stage_changed", {"from": lead.stage, "to": stage, "by": by})
    lead.stage = stage
    outbox.enqueue_crm_sync(db, settings, lead)
    return True


def deliver(db: Session, settings: Settings, lead: Lead, text: str, channel: str,
            adapters: dict[str, ChannelAdapter] | None = None) -> Message:
    """Send an agent message on ``channel`` and record it, success or failure."""
    msg = Message(lead_id=lead.id, role="agent", text=text, channel=channel,
                  delivery_status="sent", error="")
    if channel in EXTERNAL_CHANNELS:
        adapter = (adapters or build_adapters(settings))[channel]
        try:
            msg.external_id = adapter.send(lead.phone, text)
        except (ConfigurationError, IntegrationError) as exc:
            msg.delivery_status = "failed"
            msg.error = str(exc)[:1000]
            log_event(db, lead.id, "delivery_failed", {"channel": channel, "error": str(exc)})
            logger.warning("outbound delivery failed",
                           extra={"lead_id": lead.id, "channel": channel})
    elif channel != "simulator":
        msg.delivery_status = "recorded"
    db.add(msg)
    db.flush()
    return msg


@dataclass
class InboundOutcome:
    reply: str
    agent_stage: str
    duplicate: bool = False
    appointment: Appointment | None = None
    delivery_status: str = "sent"


def local_now(settings: Settings, now: datetime | None = None) -> datetime:
    return (now or utcnow()).replace(tzinfo=UTC).astimezone(settings.tz)


def _alternatives(db: Session, settings: Settings, calendar: CalendarAdapter | None,
                  now: datetime, after: datetime | None) -> list[str]:
    try:
        slots = booking.next_open_slots(db, settings.tz, settings.SLOT_MINUTES,
                                        settings.BOOKING_HORIZON_DAYS, count=3,
                                        adapter=calendar, after=after, now=now)
        if not slots and after is not None:
            slots = booking.next_open_slots(db, settings.tz, settings.SLOT_MINUTES,
                                            settings.BOOKING_HORIZON_DAYS, count=3,
                                            adapter=calendar, now=now)
    except IntegrationError as exc:
        logger.warning("availability lookup failed", extra={"error": str(exc)})
        return []
    return [booking.slot_label(s[0], settings.tz) for s in slots]


def _try_booking(db: Session, settings: Settings, lead: Lead, requested: datetime,
                 calendar: CalendarAdapter | None, now: datetime) -> tuple[str, Appointment | None]:
    start = booking.to_utc_naive(requested)
    label = booking.slot_label(start, settings.tz)
    try:
        if not booking.is_template_slot(db, start, settings.tz, settings.SLOT_MINUTES):
            raise booking.BookingError("outside availability")
        appt = booking.book(db, lead, start, slot_minutes=settings.SLOT_MINUTES,
                            adapter=calendar, now=now)
    except (booking.BookingError, IntegrationError) as exc:
        log_event(db, lead.id, "booking_unavailable",
                  {"requested": start.isoformat() + "Z", "reason": str(exc)})
        alternatives = _alternatives(db, settings, calendar, now, start)
        return agent.compose_unavailable(lead.name, label, alternatives), None
    log_event(db, lead.id, "appointment_booked", {"appointment_id": appt.id, "by": "agent"})
    set_stage(db, settings, lead, "Booked", by="agent")
    outbox.enqueue_crm_note(db, settings, lead, f"Intro call booked by ClusterX OS for {label}.")
    return agent.compose_booking_confirmation(lead.name, label), appt


def handle_inbound(
    db: Session,
    settings: Settings,
    lead: Lead,
    text: str,
    channel: str = "simulator",
    external_id: str | None = None,
    *,
    llm: LLMClient | None = None,
    calendar: CalendarAdapter | None = None,
    adapters: dict[str, ChannelAdapter] | None = None,
    now: datetime | None = None,
) -> InboundOutcome:
    """Store an inbound lead message, run the agent, book if asked, reply. Commits."""
    text = text.strip()
    if not text:
        raise ValueError("Empty message")
    if external_id:
        seen = db.scalar(select(Message.id).where(Message.channel == channel,
                                                  Message.external_id == external_id))
        if seen is not None:
            return InboundOutcome(reply="", agent_stage="", duplicate=True)

    now_utc = now or utcnow()
    history = list(db.scalars(
        select(Message.text).where(Message.lead_id == lead.id, Message.role == "lead")
        .order_by(Message.id)
    ).all())
    db.add(Message(lead_id=lead.id, role="lead", text=text, channel=channel,
                   external_id=external_id or None, delivery_status="received", error=""))
    if channel in EXTERNAL_CHANNELS:
        lead.preferred_channel = channel

    previous_intent = lead.intent
    state = agent.ConversationState(name=lead.name, stage=lead.stage,
                                    slots=json.loads(lead.slots_json or "{}"))
    result = agent.process_inbound(state, text, history, local_now(settings, now_utc), llm)

    lead.slots_json = json.dumps(result.slots)
    if result.intent:
        lead.intent, lead.intent_confidence, reasons = result.intent
        lead.intent_reasons_json = json.dumps(reasons)
    lead.opted_out = result.opted_out
    for kind, detail in result.events:
        log_event(db, lead.id, kind, detail)
    if result.kanban_stage and lead.stage in ("New", "Contacted"):
        set_stage(db, settings, lead, result.kanban_stage, by="agent")

    reply = result.reply
    appointment: Appointment | None = None
    if result.requested_time is not None:
        reply, appointment = _try_booking(db, settings, lead, result.requested_time, calendar,
                                          now_utc)
    elif result.offer_slots:
        reply = agent.compose_slot_offer(
            lead.name, _alternatives(db, settings, calendar, now_utc, None))

    if lead.intent != previous_intent:
        outbox.enqueue_crm_sync(db, settings, lead)
        if lead.intent in ("Hot", "Unqualified"):
            reasons_text = ", ".join(json.loads(lead.intent_reasons_json or "[]"))
            outbox.enqueue_crm_note(
                db, settings, lead, f"ClusterX OS classified intent as {lead.intent}: {reasons_text}")

    msg = deliver(db, settings, lead, reply, channel, adapters)
    log_event(db, lead.id, "agent_replied",
              {"channel": channel, "agent_stage": result.agent_stage, "llm": result.used_llm})
    if appointment is not None:
        booking.commit_booking(db, appointment, calendar)
    else:
        db.commit()
    return InboundOutcome(reply=reply, agent_stage=result.agent_stage, appointment=appointment,
                          delivery_status=msg.delivery_status)
