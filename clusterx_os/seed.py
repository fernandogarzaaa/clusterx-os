"""First-run bootstrap (admin user, default availability) and optional demo data."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from clusterx_os import booking
from clusterx_os.config import Settings
from clusterx_os.db import SessionLocal
from clusterx_os.models import Campaign, Enrollment, FeedToken, Lead, SlotTemplate, User, utcnow
from clusterx_os.security import hash_password
from clusterx_os.services import leads as lead_service

logger = logging.getLogger(__name__)


def bootstrap_admin(settings: Settings) -> bool:
    """On an empty database: create the first admin and Mon-Fri 09:00-17:00 availability."""
    with SessionLocal() as db:
        created = False
        if not db.scalar(select(func.count(User.id))):
            admin = User(email=settings.ADMIN_EMAIL.strip().lower(), name="Admin", role="admin",
                         is_active=True, hashed_password=hash_password(settings.ADMIN_PASSWORD))
            db.add(admin)
            db.flush()
            db.add(FeedToken(user_id=admin.id, token=secrets.token_urlsafe(32)))
            created = True
            logger.info("bootstrap admin created", extra={"email": admin.email})
        if not db.scalar(select(func.count(SlotTemplate.id))):
            for weekday in range(5):
                db.add(SlotTemplate(weekday=weekday, start_time="09:00", end_time="17:00"))
        db.commit()
        return created


DEMO_CONVERSATIONS: list[tuple[dict[str, str], str, list[str]]] = [
    ({"name": "Aarav Sharma", "email": "aarav@acme.example", "phone": "+919876543210",
      "company": "Acme Retail"}, "webhook",
     ["Hi, we are interested in a demo of your lead qualification product",
      "Our budget is around $8000 for this quarter",
      "We want to go live next month",
      "Yes, send me the proposal and pricing details"]),
    ({"name": "Priya Nair", "email": "priya@finedge.example", "phone": "+919812345678",
      "company": "FinEdge"}, "csv",
     ["Hello, maybe we could use something like this later this year",
      "Send me some info when you get a chance"]),
    ({"name": "Rahul Verma", "email": "rahul@swiftlogix.example", "phone": "+911234567890",
      "company": "SwiftLogix"}, "manual",
     ["Just looking at options right now, not sure what we need"]),
    ({"name": "Meera Iyer", "email": "meera@healthplus.example", "phone": "+919998887777",
      "company": "HealthPlus"}, "webhook",
     ["Not interested, please stop contacting me"]),
]
DEMO_PLAIN: list[tuple[dict[str, str], str, str]] = [
    ({"name": "Karan Mehta", "email": "karan@saasly.example", "phone": "+919000011122",
      "company": "SaaSly"}, "webhook", "New"),
    ({"name": "Divya Rao", "email": "divya@eduspark.example", "phone": "+919000033344",
      "company": "EduSpark"}, "manual", "Contacted"),
    ({"name": "Arjun Patel", "email": "arjun@patelmfg.example", "phone": "+919000055566",
      "company": "PatelMfg"}, "csv", "Qualified"),
    ({"name": "Sneha Kulkarni", "email": "sneha@retailmart.example", "phone": "+919000077788",
      "company": "RetailMart"}, "manual", "Lost"),
]


def seed_demo(settings: Settings) -> dict[str, Any]:
    """Idempotent demo data: conversations replayed through the real agent."""
    counts = {"leads": 0, "appointments": 0, "campaigns": 0}
    with SessionLocal() as db:
        if db.scalar(select(func.count(Lead.id))):
            return counts
        for data, source, script in DEMO_CONVERSATIONS:
            lead = lead_service.create_lead(db, settings, data, source)
            db.commit()
            for text in script:
                lead_service.handle_inbound(db, settings, lead, text, "simulator")
            counts["leads"] += 1
        for data, source, stage in DEMO_PLAIN:
            lead = lead_service.create_lead(db, settings, data, source)
            lead_service.set_stage(db, settings, lead, stage, by="seed")
            counts["leads"] += 1
        db.commit()

        hot = db.scalar(select(Lead).where(Lead.email == "arjun@patelmfg.example"))
        slots = booking.next_open_slots(db, settings.tz, settings.SLOT_MINUTES,
                                        settings.BOOKING_HORIZON_DAYS, count=1,
                                        after=utcnow() + timedelta(days=1))
        if hot is not None and slots:
            appt = booking.book(db, hot, slots[0][0], slot_minutes=settings.SLOT_MINUTES)
            lead_service.log_event(db, hot.id, "appointment_booked",
                                   {"appointment_id": appt.id, "by": "seed"})
            lead_service.set_stage(db, settings, hot, "Booked", by="seed")
            db.commit()
            counts["appointments"] += 1

        campaign = Campaign(name="No-reply follow-up", followup_hours=24, max_followups=3,
                            active=True,
                            template_text=("Hi {first_name}, just circling back on my last "
                                           "note. Still keen to speed up follow-up at {company}? "
                                           "Reply here and I can share a quick overview."))
        db.add(campaign)
        db.flush()
        quiet = db.scalar(select(Lead).where(Lead.email == "karan@saasly.example"))
        if quiet is not None:
            db.add(Enrollment(campaign_id=campaign.id, lead_id=quiet.id, status="active",
                              followups_sent=0,
                              last_agent_msg_at=utcnow() - timedelta(hours=30)))
        db.commit()
        counts["campaigns"] += 1
    logger.info("demo data seeded", extra={"counts": counts})
    return counts
