"""Leads API: CRUD, CSV import, stage changes, conversation, dashboard stats."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from clusterx_os.config import Settings
from clusterx_os.db import get_db
from clusterx_os.models import Appointment, Enrollment, Event, Lead, Message, User, utcnow
from clusterx_os.services import leads as lead_service
from clusterx_os.services.outbox import enqueue_crm_sync
from clusterx_os.web.common import get_lead_or_404, idempotent
from clusterx_os.web.deps import (
    Integrations,
    current_user,
    integrations_dep,
    rate_limit,
    require_admin,
    settings_dep,
)
from clusterx_os.web.schemas import LeadIn, LeadPatch, MessageIn, StageIn

router = APIRouter(prefix="/api",
                   dependencies=[Depends(rate_limit("api", "API_RATE_LIMIT_PER_MINUTE"))])

RANGES = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}


@router.get("/dashboard/stats")
def dashboard_stats(range: str = Query("7d", pattern="^(24h|7d|30d)$"),  # noqa: A002
                    db: Session = Depends(get_db),
                    _user: User = Depends(current_user)) -> dict[str, Any]:
    since = utcnow() - timedelta(hours=RANGES[range])
    leads = db.scalars(select(Lead).where(Lead.created_at >= since)).all()
    intent_counts = {"Hot": 0, "Warm": 0, "Cold": 0, "Unqualified": 0, "Unclassified": 0}
    outcome_counts = {"Won": 0, "Lost": 0, "In Pipeline": 0}
    for lead in leads:
        intent_counts[lead.intent if lead.intent in intent_counts else "Unclassified"] += 1
        outcome_counts[lead.stage if lead.stage in ("Won", "Lost") else "In Pipeline"] += 1
    booked = db.scalar(select(func.count(Appointment.id)).where(
        Appointment.created_at >= since, Appointment.status == "confirmed")) or 0
    return {"range": range, "total_leads": len(leads), "intent_counts": intent_counts,
            "outcome_counts": outcome_counts, "appointments_booked": int(booked)}


@router.get("/leads")
def list_leads(stage: str | None = None, intent: str | None = None,
               q: str | None = Query(None, max_length=100),
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db),
               _user: User = Depends(current_user)) -> dict[str, Any]:
    query = select(Lead)
    if stage:
        query = query.where(Lead.stage == stage)
    if intent:
        query = query.where(Lead.intent == intent)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(or_(func.lower(Lead.name).like(like), Lead.email.like(like),
                                func.lower(Lead.company).like(like), Lead.phone.like(like)))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(Lead.created_at.desc(), Lead.id.desc())
                      .limit(limit).offset(offset)).all()
    return {"total": int(total), "limit": limit, "offset": offset,
            "leads": [lead_service.serialize_lead(r) for r in rows]}


@router.post("/leads", status_code=201)
def create_lead(payload: LeadIn, request: Request, db: Session = Depends(get_db),
                settings: Settings = Depends(settings_dep),
                _user: User = Depends(current_user)) -> JSONResponse:
    def handler() -> tuple[int, Any]:
        try:
            lead = lead_service.create_lead(db, settings, payload.model_dump(),
                                            payload.source or "manual")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.commit()
        return 201, lead_service.serialize_lead(lead)

    return idempotent(request, db, "leads.create", payload.model_dump(), handler)


@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, db: Session = Depends(get_db),
             _user: User = Depends(current_user)) -> dict[str, Any]:
    lead = get_lead_or_404(db, lead_id)
    events = db.scalars(select(Event).where(Event.lead_id == lead_id)
                        .order_by(Event.id.desc()).limit(100)).all()
    data = lead_service.serialize_lead(lead)
    data["events"] = [{"kind": e.kind, "detail": json.loads(e.detail_json),
                       "created_at": e.created_at.isoformat() + "Z"} for e in events]
    return data


@router.patch("/leads/{lead_id}")
def update_lead(lead_id: int, payload: LeadPatch, db: Session = Depends(get_db),
                settings: Settings = Depends(settings_dep),
                _user: User = Depends(current_user)) -> dict[str, Any]:
    lead = get_lead_or_404(db, lead_id)
    changes = payload.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(lead, key, value)
    if changes:
        lead_service.log_event(db, lead.id, "lead_updated", {"fields": sorted(changes)})
        enqueue_crm_sync(db, settings, lead)
    db.commit()
    return lead_service.serialize_lead(lead)


@router.delete("/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: int, db: Session = Depends(get_db),
                _admin: User = Depends(require_admin)) -> Response:
    """Hard-delete a lead and all of its data (right-to-erasure requests)."""
    get_lead_or_404(db, lead_id)
    for model in (Message, Event, Appointment, Enrollment):
        db.execute(delete(model).where(model.lead_id == lead_id))
    db.execute(delete(Lead).where(Lead.id == lead_id))
    db.commit()
    return Response(status_code=204)


@router.post("/leads/import-csv")
def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db),
               settings: Settings = Depends(settings_dep),
               _user: User = Depends(current_user)) -> dict[str, Any]:
    raw = file.file.read(settings.MAX_UPLOAD_BYTES + 1)
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV file too large")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    imported = updated = 0
    errors: list[dict[str, Any]] = []
    for line, row in enumerate(reader, start=2):
        clean = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not any(clean.get(f) for f in ("name", "email", "phone")):
            continue
        try:
            data = LeadIn(**{f: clean.get(f, "") for f in ("name", "email", "phone", "company")})
            _lead, created = lead_service.upsert_lead(db, settings, data.model_dump(), "csv")
        except (ValidationError, ValueError) as exc:
            errors.append({"line": line, "error": str(exc).splitlines()[0][:200]})
            continue
        imported += int(created)
        updated += int(not created)
    db.commit()
    return {"imported": imported, "updated": updated, "errors": errors[:100]}


@router.post("/leads/{lead_id}/stage")
def set_stage(lead_id: int, payload: StageIn, db: Session = Depends(get_db),
              settings: Settings = Depends(settings_dep),
              user: User = Depends(current_user)) -> dict[str, Any]:
    lead = get_lead_or_404(db, lead_id)
    lead_service.set_stage(db, settings, lead, payload.stage, by=user.email)
    db.commit()
    return lead_service.serialize_lead(lead)


@router.get("/leads/{lead_id}/messages")
def list_messages(lead_id: int, db: Session = Depends(get_db),
                  _user: User = Depends(current_user)) -> dict[str, Any]:
    get_lead_or_404(db, lead_id)
    msgs = db.scalars(select(Message).where(Message.lead_id == lead_id).order_by(Message.id)).all()
    return {"messages": [{"id": m.id, "role": m.role, "text": m.text, "channel": m.channel,
                          "delivery_status": m.delivery_status, "error": m.error or None,
                          "created_at": m.created_at.isoformat() + "Z"} for m in msgs]}


@router.post("/leads/{lead_id}/messages")
def simulate_inbound(lead_id: int, payload: MessageIn, db: Session = Depends(get_db),
                     settings: Settings = Depends(settings_dep),
                     integrations: Integrations = Depends(integrations_dep),
                     _user: User = Depends(current_user)) -> dict[str, Any]:
    """Post a message *as the lead* (in-app simulator); the agent replies."""
    lead = get_lead_or_404(db, lead_id)
    outcome = lead_service.handle_inbound(
        db, settings, lead, payload.text, "simulator", llm=integrations.llm,
        calendar=integrations.calendar, adapters=integrations.channels)
    return {"reply": outcome.reply, "agent_stage": outcome.agent_stage,
            "appointment_id": outcome.appointment.id if outcome.appointment else None,
            "lead": lead_service.serialize_lead(lead)}


@router.post("/leads/{lead_id}/send")
def send_manual(lead_id: int, payload: MessageIn, db: Session = Depends(get_db),
                settings: Settings = Depends(settings_dep),
                integrations: Integrations = Depends(integrations_dep),
                user: User = Depends(current_user)) -> dict[str, Any]:
    """Send a message from a human teammate to the lead on its channel."""
    lead = get_lead_or_404(db, lead_id)
    if lead.opted_out:
        raise HTTPException(status_code=409, detail="Lead opted out of messages")
    channel = lead.preferred_channel or settings.OUTBOUND_CHANNEL
    msg = lead_service.deliver(db, settings, lead, payload.text, channel, integrations.channels)
    lead_service.log_event(db, lead.id, "human_message", {"by": user.email, "channel": channel})
    db.commit()
    return {"id": msg.id, "channel": channel, "delivery_status": msg.delivery_status,
            "error": msg.error or None}
