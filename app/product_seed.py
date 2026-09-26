"""Demo data for ClusterX OS. Idempotent: safe to run more than once."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import agent as agent_logic
from app import calendar_util, models
from app.ai.providers import LocalProvider


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_lead(db: Session, **kwargs) -> models.Lead:
    lead = models.Lead(**kwargs)
    db.add(lead)
    db.flush()
    db.add(models.Event(lead_id=lead.id, kind="lead_created",
                        detail_json=json.dumps({"source": lead.source})))
    return lead


def _converse(db: Session, lead: models.Lead, texts: list[str]) -> None:
    """Replay a scripted conversation through the real agent."""
    provider = LocalProvider()
    for text in texts:
        db.add(models.Message(lead_id=lead.id, role="lead", text=text,
                              channel="simulator"))
        db.flush()
        history = [
            m.text for m in db.query(models.Message)
            .filter(models.Message.lead_id == lead.id,
                    models.Message.role == "lead")
            .order_by(models.Message.id).all()
        ][:-1]
        result = agent_logic.process_inbound(lead, text, provider, history)
        slots = json.loads(lead.slots_json or "{}")
        slots.update(result.slot_updates)
        lead.slots_json = json.dumps(slots)
        if result.kanban_stage:
            lead.stage = result.kanban_stage
        if result.intent:
            lead.intent, lead.intent_confidence, reasons = result.intent
            lead.intent_reasons_json = json.dumps(reasons)
        db.add(models.Message(lead_id=lead.id, role="agent",
                              text=result.reply, channel="simulator"))
        for kind, detail in result.events:
            db.add(models.Event(lead_id=lead.id, kind=kind,
                                detail_json=json.dumps(detail)))
        db.flush()


def seed_product(db: Session) -> dict:
    counts = {"slot_templates": 0, "leads": 0, "appointments": 0,
              "campaigns": 0, "feed_tokens": 0}

    if db.query(models.SlotTemplate).count() == 0:
        for weekday in range(5):  # Mon-Fri
            db.add(models.SlotTemplate(weekday=weekday, start_time="09:00",
                                       end_time="17:00"))
            counts["slot_templates"] += 1

    if db.query(models.Lead).count() == 0:
        # Full greet -> book conversation.
        aarav = _add_lead(
            db, name="Aarav Sharma", email="aarav@acme.in",
            phone="+919876543210", company="Acme Retail",
            source="webhook", stage="New",
        )
        _converse(db, aarav, [
            "Hi, we are interested in a demo of your lead qualification product",
            "We run a 40 person sales team and need faster follow up",
            "Our budget is around $8000 for this quarter",
            "We want to go live next month",
            "Yes, send me the proposal and pricing details",
        ])
        counts["leads"] += 1

        priya = _add_lead(
            db, name="Priya Nair", email="priya@fintech.co",
            phone="+919812345678", company="FinEdge",
            source="csv", stage="New",
        )
        _converse(db, priya, [
            "Hello, maybe we could use something like this later this year",
            "Send me some info when you get a chance",
        ])
        counts["leads"] += 1

        rahul = _add_lead(
            db, name="Rahul Verma", email="rahul@logistics.io",
            phone="+911234567890", company="SwiftLogix",
            source="manual", stage="New",
        )
        _converse(db, rahul, [
            "Just looking at options right now, not sure what we need",
        ])
        counts["leads"] += 1

        meera = _add_lead(
            db, name="Meera Iyer", email="meera@healthplus.org",
            phone="+919998887777", company="HealthPlus",
            source="webhook", stage="New",
        )
        _converse(db, meera, [
            "Not interested, please stop contacting me",
        ])
        counts["leads"] += 1

        # Leads spread across kanban stages without full conversations.
        specs = [
            ("Karan Mehta", "karan@saasly.com", "+919000011122", "SaaSly",
             "webhook", "New", None),
            ("Divya Rao", "divya@edtech.in", "+919000033344", "EduSpark",
             "manual", "Contacted", "Warm"),
            ("Arjun Patel", "arjun@manufacture.co", "+919000055566", "PatelMfg",
             "csv", "Qualified", "Hot"),
            ("Sneha Kulkarni", "sneha@retailmart.in", "+919000077788", "RetailMart",
             "manual", "Lost", "Cold"),
        ]
        for name, email, phone, company, source, stage, intent in specs:
            lead = _add_lead(db, name=name, email=email, phone=phone,
                             company=company, source=source, stage=stage,
                             intent=intent)
            if intent:
                lead.intent_confidence = 0.75
                lead.intent_reasons_json = json.dumps(
                    [f"seeded as {intent.lower()} example"])
            counts["leads"] += 1

        # One booked appointment for the hot lead (tomorrow 10:00 UTC).
        hot = db.query(models.Lead).filter(
            models.Lead.email == "arjun@manufacture.co").first()
        start = (_now() + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        appt = calendar_util.book_appointment(
            db, hot.id, "Intro call with Arjun Patel (PatelMfg)", start,
            attendee_email=hot.email,
        )
        hot.stage = "Booked"
        db.add(models.Event(lead_id=hot.id, kind="appointment_booked",
                            detail_json=json.dumps({"appointment_id": appt.id})))
        counts["appointments"] += 1

    if db.query(models.Campaign).count() == 0:
        campaign = models.Campaign(
            name="No-reply follow-up",
            template_text=(
                "Hi {name}, just circling back on my last note. "
                "Still interested in improving follow-up at {company}? "
                "Reply here and I can share a quick overview."
            ),
            followup_hours=24,
            active=True,
        )
        db.add(campaign)
        db.flush()
        # Enroll a quiet lead so the scheduler has something real to do.
        quiet = db.query(models.Lead).filter(
            models.Lead.email == "karan@saasly.com").first()
        if quiet:
            db.add(models.Enrollment(
                campaign_id=campaign.id, lead_id=quiet.id,
                last_agent_msg_at=_now() - timedelta(hours=30),
                status="active",
            ))
        counts["campaigns"] += 1

    admin = db.query(models.User).filter(
        models.User.email == "admin@clusterx.local").first()
    if admin and db.query(models.FeedToken).filter(
            models.FeedToken.user_id == admin.id).count() == 0:
        db.add(models.FeedToken(user_id=admin.id,
                                token=secrets.token_urlsafe(32)))
        counts["feed_tokens"] += 1

    db.commit()
    return counts
