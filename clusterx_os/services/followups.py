"""Campaign follow-ups: nudge enrolled leads who went quiet."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clusterx_os.config import Settings
from clusterx_os.integrations.channels import ChannelAdapter
from clusterx_os.models import Campaign, Enrollment, Lead, Message, utcnow
from clusterx_os.services.leads import deliver, log_event

logger = logging.getLogger(__name__)

STOP_STAGES = ("Booked", "Won", "Lost")


def render_template(template: str, lead: Lead) -> str:
    parts = lead.name.split()
    first = parts[0] if parts else "there"
    return (template.replace("{first_name}", first)
            .replace("{name}", lead.name or "there")
            .replace("{company}", lead.company or "your company"))


def stop_reason(lead: Lead, enrollment: Enrollment, campaign: Campaign) -> str | None:
    if lead.opted_out or lead.intent == "Unqualified":
        return "opted_out"
    if lead.stage in STOP_STAGES:
        return f"stage_{lead.stage.lower()}"
    if enrollment.followups_sent >= campaign.max_followups:
        return "max_followups"
    return None


def run_due_followups(db: Session, settings: Settings, now: datetime | None = None,
                      campaign_id: int | None = None,
                      adapters: dict[str, ChannelAdapter] | None = None) -> list[dict[str, Any]]:
    """Send due follow-ups. Commits. Returns one entry per message sent."""
    now = now or utcnow()
    sent: list[dict[str, Any]] = []
    query = select(Campaign).where(Campaign.active.is_(True))
    if campaign_id is not None:
        query = query.where(Campaign.id == campaign_id)
    for campaign in db.scalars(query).all():
        enrollments = db.scalars(select(Enrollment).where(
            Enrollment.campaign_id == campaign.id, Enrollment.status == "active")).all()
        for enr in enrollments:
            lead = db.get(Lead, enr.lead_id)
            if lead is None:
                enr.status = "stopped"
                continue
            reason = stop_reason(lead, enr, campaign)
            if reason:
                enr.status = "completed" if reason == "max_followups" else "stopped"
                log_event(db, lead.id, "campaign_ended",
                          {"campaign_id": campaign.id, "reason": reason})
                continue
            last = enr.last_agent_msg_at
            if last is not None:
                if now - last < timedelta(hours=campaign.followup_hours):
                    continue
                replied = db.scalar(select(Message.id).where(
                    Message.lead_id == lead.id, Message.role == "lead",
                    Message.created_at > last).limit(1))
                if replied is not None:
                    enr.last_agent_msg_at = now  # conversation is alive; restart the clock
                    continue
            channel = lead.preferred_channel or settings.OUTBOUND_CHANNEL
            msg = deliver(db, settings, lead, render_template(campaign.template_text, lead),
                          channel, adapters)
            if msg.delivery_status == "failed":
                continue  # retried on the next tick
            enr.last_agent_msg_at = now
            enr.followups_sent += 1
            log_event(db, lead.id, "followup_sent",
                      {"campaign_id": campaign.id, "channel": channel,
                       "number": enr.followups_sent})
            sent.append({"lead_id": lead.id, "campaign_id": campaign.id, "channel": channel})
    db.commit()
    return sent
