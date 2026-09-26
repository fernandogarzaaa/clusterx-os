"""Follow-up scheduler.

run_due_followups(db) is the deterministic core: enrollments in active
campaigns where the agent's last message is older than followup_hours (and
the lead has not replied since) get the campaign template sent. Tests call
it directly. The APScheduler job below only runs when RUN_SCHEDULER=1.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import models
from app.channels import get_adapter

DEFAULT_CHANNEL = os.getenv("OUTBOUND_CHANNEL", "simulator")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _render(template: str, lead: models.Lead) -> str:
    return (
        template.replace("{name}", lead.name or "there")
        .replace("{company}", lead.company or "your company")
    )


def run_due_followups(
    db: Session, now: datetime | None = None, campaign_id: int | None = None
) -> list[dict]:
    """Send due campaign follow-ups. Returns a list of what was sent."""
    now = (now.replace(tzinfo=None) if now else _now())
    sent: list[dict] = []
    query = db.query(models.Campaign).filter(models.Campaign.active.is_(True))
    if campaign_id is not None:
        query = query.filter(models.Campaign.id == campaign_id)
    campaigns = query.all()
    for campaign in campaigns:
        enrollments = (
            db.query(models.Enrollment)
            .filter(
                models.Enrollment.campaign_id == campaign.id,
                models.Enrollment.status == "active",
            )
            .all()
        )
        for enr in enrollments:
            lead = db.query(models.Lead).filter(models.Lead.id == enr.lead_id).first()
            if not lead:
                continue
            last_agent = enr.last_agent_msg_at
            if last_agent is None:
                due = True
            else:
                due = (now - last_agent.replace(tzinfo=None)) >= timedelta(
                hours=campaign.followup_hours
            )
            if not due:
                continue
            # Skip if the lead replied after the agent's last message.
            if last_agent is not None:
                reply_since = (
                    db.query(models.Message)
                    .filter(
                        models.Message.lead_id == lead.id,
                        models.Message.role == "lead",
                        models.Message.created_at > last_agent,
                    )
                    .first()
                )
                if reply_since:
                    enr.last_agent_msg_at = now
                    continue
            text = _render(campaign.template_text, lead)
            adapter = get_adapter(DEFAULT_CHANNEL)
            adapter.send(db, lead, text)
            # Mark channel as campaign when the simulator recorded it.
            msg = (
                db.query(models.Message)
                .filter(models.Message.lead_id == lead.id)
                .order_by(models.Message.id.desc())
                .first()
            )
            if msg and msg.channel == "simulator":
                msg.channel = "campaign"
            enr.last_agent_msg_at = now
            db.add(
                models.Event(
                    lead_id=lead.id,
                    kind="followup_sent",
                    detail_json=(
                        '{"campaign_id": %d, "channel": "%s"}'
                        % (campaign.id, adapter.name)
                    ),
                )
            )
            sent.append(
                {"lead_id": lead.id, "campaign_id": campaign.id, "channel": adapter.name}
            )
    db.commit()
    return sent


def start_scheduler():
    """Start the APScheduler background job. Called from lifespan only."""
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.core.db import SessionLocal

    def _tick() -> None:
        db = SessionLocal()
        try:
            run_due_followups(db)
        except Exception:
            db.rollback()
        finally:
            db.close()

    scheduler = BackgroundScheduler()
    scheduler.add_job(_tick, "interval", seconds=60, id="followups", replace_existing=True)
    scheduler.start()
    return scheduler
