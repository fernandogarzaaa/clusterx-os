"""Co-pilot: natural-language Q&A over lead/CRM data.

Regex-based question parser backed by real DB queries. Returns
{"answer": str, "rows": [dict]}. Unknown questions get an honest
"I can't answer that yet" with the list of supported patterns.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import models

STAGES = ["New", "Contacted", "Qualified", "Booked", "Won", "Lost"]
INTENTS = ["Hot", "Warm", "Cold", "Unqualified"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lead_row(lead: models.Lead) -> dict:
    return {
        "id": lead.id,
        "name": lead.name,
        "company": lead.company,
        "email": lead.email,
        "stage": lead.stage,
        "intent": lead.intent,
        "created_at": str(lead.created_at),
    }


def answer_question(db: Session, question: str) -> dict:
    q = question.strip().lower()
    rows: list[dict] = []

    # how many hot leads / how many warm leads / ...
    m = re.search(r"how many (\w+) leads", q)
    if m:
        word = m.group(1).capitalize()
        if word in INTENTS:
            n = db.query(models.Lead).filter(models.Lead.intent == word).count()
            return {"answer": f"There are {n} {word.lower()} leads.", "rows": rows}
        stage = next((s for s in STAGES if s.lower() == word.lower()), None)
        if stage:
            n = db.query(models.Lead).filter(models.Lead.stage == stage).count()
            return {"answer": f"There are {n} leads in stage '{stage}'.", "rows": rows}

    # how many leads / total leads / lead count
    if re.search(r"\bhow many leads\b|\btotal leads\b|\blead count\b", q):
        n = db.query(models.Lead).count()
        by_stage = {
            s: db.query(models.Lead).filter(models.Lead.stage == s).count()
            for s in STAGES
        }
        breakdown = ", ".join(f"{s}: {c}" for s, c in by_stage.items() if c)
        answer = f"There are {n} leads total."
        if breakdown:
            answer += f" By stage: {breakdown}."
        return {"answer": answer, "rows": rows}

    # how many leads in <stage>
    m = re.search(r"leads in (?:stage )?(\w+)", q)
    if m:
        stage = next((s for s in STAGES if s.lower() == m.group(1).lower()), None)
        if stage:
            n = db.query(models.Lead).filter(models.Lead.stage == stage).count()
            return {"answer": f"There are {n} leads in stage '{stage}'.", "rows": rows}

    # show/list leads from last week / this week / today / yesterday
    m = re.search(r"(?:show|list) leads from (last week|this week|today|yesterday)", q)
    if m:
        window = m.group(1)
        now = _now()
        if window == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif window == "yesterday":
            start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            leads = (
                db.query(models.Lead)
                .filter(models.Lead.created_at >= start, models.Lead.created_at < end)
                .order_by(models.Lead.created_at.desc())
                .all()
            )
            rows = [_lead_row(lead) for lead in leads]
            return {"answer": f"{len(rows)} leads from yesterday.", "rows": rows}
        elif window == "this week":
            start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:  # last week
            start = now - timedelta(days=7)
        leads = (
            db.query(models.Lead)
            .filter(models.Lead.created_at >= start)
            .order_by(models.Lead.created_at.desc())
            .all()
        )
        rows = [_lead_row(lead) for lead in leads]
        return {"answer": f"{len(rows)} leads from {window}.", "rows": rows}

    # leads with no reply / unresponsive leads
    if re.search(r"no reply|unresponsive|never replied|no response", q):
        replied_ids = {
            r[0]
            for r in db.query(models.Message.lead_id)
            .filter(models.Message.role == "lead")
            .distinct()
            .all()
        }
        leads = db.query(models.Lead).order_by(models.Lead.created_at.desc()).all()
        quiet = [lead for lead in leads if lead.id not in replied_ids]
        rows = [_lead_row(lead) for lead in quiet]
        return {"answer": f"{len(rows)} leads have never replied.", "rows": rows}

    # booked appointments this week / upcoming appointments
    if re.search(r"appointment", q):
        now = _now()
        if "this week" in q or "upcoming" in q:
            end = now + timedelta(days=7)
            appts = (
                db.query(models.Appointment)
                .filter(
                    models.Appointment.starts_at >= now,
                    models.Appointment.starts_at < end,
                )
                .order_by(models.Appointment.starts_at)
                .all()
            )
            rows = [
                {
                    "id": a.id,
                    "title": a.title,
                    "starts_at": str(a.starts_at),
                    "attendee": a.attendee_email,
                }
                for a in appts
            ]
            return {
                "answer": f"{len(rows)} appointments in the next 7 days.",
                "rows": rows,
            }
        n = db.query(models.Appointment).count()
        return {"answer": f"There are {n} appointments booked.", "rows": rows}

    # intent breakdown
    if re.search(r"intent breakdown|leads by intent", q):
        parts = []
        for intent in INTENTS:
            n = db.query(models.Lead).filter(models.Lead.intent == intent).count()
            parts.append(f"{intent}: {n}")
        return {"answer": "Leads by intent: " + ", ".join(parts) + ".", "rows": rows}

    # which leads are hot / show hot leads
    if re.search(r"(?:show|list|which).*(hot|warm|cold) leads", q) or re.search(
        r"^(hot|warm|cold) leads$", q.strip()
    ):
        m2 = re.search(r"(hot|warm|cold)", q)
        intent = m2.group(1).capitalize()
        leads = (
            db.query(models.Lead)
            .filter(models.Lead.intent == intent)
            .order_by(models.Lead.created_at.desc())
            .all()
        )
        rows = [_lead_row(lead) for lead in leads]
        return {"answer": f"{len(rows)} {intent.lower()} leads.", "rows": rows}

    return {
        "answer": (
            "I can't answer that yet. Try: 'how many hot leads?', "
            "'how many leads in Qualified?', 'show leads from last week', "
            "'leads with no reply', 'booked appointments this week', "
            "'intent breakdown', or 'how many leads?'."
        ),
        "rows": [],
    }
