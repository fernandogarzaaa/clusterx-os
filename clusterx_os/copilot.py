"""Co-pilot: natural-language questions over the pipeline, answered from the database.

Deterministic pattern matching backed by real queries; unknown questions get
an honest "can't answer yet" with the supported phrasings.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from clusterx_os.models import INTENTS, KANBAN_STAGES, Appointment, Lead, Message, utcnow

HELP = ("I can't answer that yet. Try: 'how many hot leads?', 'how many leads in Qualified?', "
        "'show leads from last week', 'leads with no reply', 'upcoming appointments', "
        "'intent breakdown', 'conversion rate', or 'how many leads?'.")


def _lead_row(lead: Lead) -> dict[str, Any]:
    return {"id": lead.id, "name": lead.name, "company": lead.company, "email": lead.email,
            "stage": lead.stage, "intent": lead.intent,
            "created_at": lead.created_at.isoformat() + "Z"}


def _count(db: Session, *where: ColumnElement[bool]) -> int:
    return int(db.scalar(select(func.count(Lead.id)).where(*where)) or 0)


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def window(name: str, now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of a named window, in UTC."""
    today = _midnight(now)
    if name == "today":
        return today, now + timedelta(seconds=1)
    if name == "yesterday":
        return today - timedelta(days=1), today
    week_start = today - timedelta(days=today.weekday())
    if name == "this week":
        return week_start, now + timedelta(seconds=1)
    if name == "last week":
        return week_start - timedelta(days=7), week_start
    return now - timedelta(days=7), now + timedelta(seconds=1)  # "last 7 days"


def _stage(word: str) -> str | None:
    return next((s for s in KANBAN_STAGES if s.lower() == word.lower()), None)


def answer_question(db: Session, question: str, now: datetime | None = None) -> dict[str, Any]:
    q = question.strip().lower().rstrip("?!. ")
    now = now or utcnow()

    m = re.search(r"how many (\w+) leads", q)
    if m:
        word = m.group(1).capitalize()
        if word in INTENTS:
            n = _count(db, Lead.intent == word)
            return {"answer": f"There are {n} {word.lower()} leads.", "rows": []}
        stage = _stage(word)
        if stage:
            n = _count(db, Lead.stage == stage)
            return {"answer": f"There are {n} leads in stage '{stage}'.", "rows": []}

    m = re.search(r"leads (?:in|at) (?:stage )?(\w+)", q)
    stage = _stage(m.group(1)) if m else None
    if stage:
        n = _count(db, Lead.stage == stage)
        return {"answer": f"There are {n} leads in stage '{stage}'.", "rows": []}

    if re.search(r"\bhow many leads\b|\btotal leads\b|\blead count\b", q):
        n = _count(db)
        parts = [f"{s}: {c}" for s in KANBAN_STAGES if (c := _count(db, Lead.stage == s))]
        answer = f"There are {n} leads total."
        if parts:
            answer += f" By stage: {', '.join(parts)}."
        return {"answer": answer, "rows": []}

    m = re.search(r"(?:show|list|which) leads (?:from|created|added)?\s*"
                  r"(last 7 days|last week|this week|today|yesterday)", q)
    if m:
        start, end = window(m.group(1), now)
        leads = db.scalars(select(Lead).where(Lead.created_at >= start, Lead.created_at < end)
                           .order_by(Lead.created_at.desc())).all()
        rows = [_lead_row(lead) for lead in leads]
        return {"answer": f"{len(rows)} leads from {m.group(1)}.", "rows": rows}

    if re.search(r"no reply|unresponsive|never replied|no response", q):
        replied = select(Message.lead_id).where(Message.role == "lead").distinct()
        leads = db.scalars(select(Lead).where(Lead.id.not_in(replied))
                           .order_by(Lead.created_at.desc())).all()
        rows = [_lead_row(lead) for lead in leads]
        return {"answer": f"{len(rows)} leads have never replied.", "rows": rows}

    if "appointment" in q or "meeting" in q or "calls booked" in q:
        if any(w in q for w in ("this week", "upcoming", "next", "scheduled")):
            appts = db.scalars(select(Appointment).where(
                Appointment.status == "confirmed", Appointment.starts_at >= now,
                Appointment.starts_at < now + timedelta(days=7)).order_by(Appointment.starts_at)
            ).all()
            rows = [{"id": a.id, "title": a.title, "starts_at": a.starts_at.isoformat() + "Z",
                     "attendee": a.attendee_email} for a in appts]
            return {"answer": f"{len(rows)} appointments in the next 7 days.", "rows": rows}
        n = int(db.scalar(select(func.count(Appointment.id))
                          .where(Appointment.status == "confirmed")) or 0)
        return {"answer": f"There are {n} confirmed appointments.", "rows": []}

    if re.search(r"intent breakdown|by intent", q):
        parts = [f"{i}: {_count(db, Lead.intent == i)}" for i in INTENTS]
        return {"answer": "Leads by intent: " + ", ".join(parts) + ".", "rows": []}

    if re.search(r"conversion|win rate|booking rate", q):
        total = _count(db)
        if total == 0:
            return {"answer": "No leads yet, so no conversion rate.", "rows": []}
        booked = _count(db, Lead.stage.in_(("Booked", "Won")))
        won = _count(db, Lead.stage == "Won")
        return {"answer": (f"{booked}/{total} leads booked or won ({booked / total:.0%}); "
                           f"{won} won ({won / total:.0%})."), "rows": []}

    m = re.search(r"\b(hot|warm|cold|unqualified) leads\b", q)
    if m:
        intent = m.group(1).capitalize()
        leads = db.scalars(select(Lead).where(Lead.intent == intent)
                           .order_by(Lead.created_at.desc())).all()
        rows = [_lead_row(lead) for lead in leads]
        return {"answer": f"{len(rows)} {intent.lower()} leads.", "rows": rows}

    return {"answer": HELP, "rows": []}
