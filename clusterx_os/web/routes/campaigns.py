"""Campaigns API: follow-up sequences for quiet leads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clusterx_os.config import Settings
from clusterx_os.db import get_db
from clusterx_os.models import Campaign, Enrollment, Lead, User, utcnow
from clusterx_os.services.followups import run_due_followups
from clusterx_os.services.leads import log_event
from clusterx_os.web.deps import Integrations, current_user, integrations_dep, settings_dep
from clusterx_os.web.schemas import CampaignIn, CampaignPatch, EnrollIn

router = APIRouter(prefix="/api/campaigns")


def serialize_campaign(c: Campaign, enrolled: int = 0) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "template_text": c.template_text,
            "followup_hours": c.followup_hours, "max_followups": c.max_followups,
            "active": c.active, "active_enrollments": enrolled}


def _get(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.get("")
def list_campaigns(db: Session = Depends(get_db),
                   _user: User = Depends(current_user)) -> dict[str, Any]:
    out = []
    for c in db.scalars(select(Campaign).order_by(Campaign.id)):
        n = db.scalar(select(func.count(Enrollment.id)).where(
            Enrollment.campaign_id == c.id, Enrollment.status == "active")) or 0
        out.append(serialize_campaign(c, int(n)))
    return {"campaigns": out}


@router.post("", status_code=201)
def create_campaign(payload: CampaignIn, db: Session = Depends(get_db),
                    _user: User = Depends(current_user)) -> dict[str, Any]:
    campaign = Campaign(**payload.model_dump(), active=True)
    db.add(campaign)
    db.commit()
    return serialize_campaign(campaign)


@router.patch("/{campaign_id}")
def update_campaign(campaign_id: int, payload: CampaignPatch, db: Session = Depends(get_db),
                    _user: User = Depends(current_user)) -> dict[str, Any]:
    campaign = _get(db, campaign_id)
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(campaign, key, value)
    db.commit()
    return serialize_campaign(campaign)


@router.post("/{campaign_id}/enroll")
def enroll_leads(campaign_id: int, payload: EnrollIn, db: Session = Depends(get_db),
                 _user: User = Depends(current_user)) -> dict[str, Any]:
    _get(db, campaign_id)
    enrolled, skipped = 0, []
    for lead_id in dict.fromkeys(payload.lead_ids):
        lead = db.get(Lead, lead_id)
        exists = db.scalar(select(Enrollment.id).where(
            Enrollment.campaign_id == campaign_id, Enrollment.lead_id == lead_id,
            Enrollment.status == "active"))
        if lead is None or lead.opted_out or exists is not None:
            skipped.append(lead_id)
            continue
        db.add(Enrollment(campaign_id=campaign_id, lead_id=lead_id, followups_sent=0,
                          last_agent_msg_at=utcnow(), status="active"))
        log_event(db, lead_id, "campaign_enrolled", {"campaign_id": campaign_id})
        enrolled += 1
    db.commit()
    return {"enrolled": enrolled, "skipped": skipped}


@router.post("/{campaign_id}/run-now")
def campaign_run_now(campaign_id: int, db: Session = Depends(get_db),
                     settings: Settings = Depends(settings_dep),
                     integrations: Integrations = Depends(integrations_dep),
                     _user: User = Depends(current_user)) -> dict[str, Any]:
    _get(db, campaign_id)
    sent = run_due_followups(db, settings, campaign_id=campaign_id,
                             adapters=integrations.channels)
    return {"sent": sent, "count": len(sent)}
