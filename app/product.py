"""ClusterX OS product router: agentic lead-qualification OS."""

from __future__ import annotations

import csv
import hmac
import io
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import agent as agent_logic
from app import calendar_util, copilot, models
from app.ai.providers import get_provider
from app.channels import adapter_statuses, get_adapter
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, page_or_login
from app.scheduler import run_due_followups

router = APIRouter()
templates = Jinja2Templates(directory="templates")

WEBHOOK_API_KEY = os.getenv("WEBHOOK_API_KEY", "dev-webhook-key")


def _ctx(request: Request, user: models.User, **extra) -> dict:
    return {"request": request, "user": user, "app_name": settings.APP_NAME, **extra}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _page_user(request: Request, db: Session):
    auth = page_or_login(request, db)
    if isinstance(auth, RedirectResponse):
        return None, auth
    return auth, None


def _log(db: Session, lead_id: int, kind: str, detail: dict | None = None) -> None:
    db.add(models.Event(
        lead_id=lead_id, kind=kind, detail_json=json.dumps(detail or {})))
    db.flush()


def _serialize_lead(lead: models.Lead) -> dict:
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
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


# ---------------------------------------------------------------- pages ---

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    range: str = Query("7d", pattern="^(24h|7d|30d)$"),
):
    user, redir = _page_user(request, db)
    if redir:
        return redir
    return templates.TemplateResponse(
        request, "dashboard.html", _ctx(request, user, range=range))


@router.get("/leads", response_class=HTMLResponse)
def leads_board(request: Request, db: Session = Depends(get_db)):
    user, redir = _page_user(request, db)
    if redir:
        return redir
    leads = db.query(models.Lead).order_by(models.Lead.created_at.desc()).all()
    board: dict[str, list] = {s: [] for s in agent_logic.KANBAN_STAGES}
    for lead in leads:
        board.setdefault(lead.stage, []).append(lead)
    return templates.TemplateResponse(
        request, "leads.html",
        _ctx(request, user, board=board, stages=agent_logic.KANBAN_STAGES))


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_db)):
    user, redir = _page_user(request, db)
    if redir:
        return redir
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    messages = (
        db.query(models.Message)
        .filter(models.Message.lead_id == lead_id)
        .order_by(models.Message.id)
        .all()
    )
    events = (
        db.query(models.Event)
        .filter(models.Event.lead_id == lead_id)
        .order_by(models.Event.id.desc())
        .limit(50)
        .all()
    )
    appointments = (
        db.query(models.Appointment)
        .filter(models.Appointment.lead_id == lead_id)
        .order_by(models.Appointment.starts_at)
        .all()
    )
    try:
        reasons = json.loads(lead.intent_reasons_json or "[]")
    except json.JSONDecodeError:
        reasons = []
    return templates.TemplateResponse(
        request, "lead_detail.html",
        _ctx(request, user, lead=lead, messages=messages, events=events,
             appointments=appointments, reasons=reasons,
             stages=agent_logic.KANBAN_STAGES))


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    db: Session = Depends(get_db),
    date: str = Query(default=""),
):
    user, redir = _page_user(request, db)
    if redir:
        return redir
    day = date or _now().date().isoformat()
    bookings = (
        db.query(models.Appointment)
        .filter(models.Appointment.starts_at >= _now())
        .order_by(models.Appointment.starts_at)
        .limit(50)
        .all()
    )
    leads = db.query(models.Lead).order_by(models.Lead.name).all()
    token = (
        db.query(models.FeedToken)
        .filter(models.FeedToken.user_id == user.id)
        .first()
    )
    return templates.TemplateResponse(
        request, "calendar.html",
        _ctx(request, user, day=day, bookings=bookings, leads=leads,
             feed_token=token.token if token else ""))


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, db: Session = Depends(get_db)):
    user, redir = _page_user(request, db)
    if redir:
        return redir
    campaigns = db.query(models.Campaign).order_by(models.Campaign.id).all()
    enrollments = db.query(models.Enrollment).all()
    enrolled_by_campaign: dict[int, list] = {}
    for enr in enrollments:
        enrolled_by_campaign.setdefault(enr.campaign_id, []).append(enr)
    lead_names = {lead.id: lead.name for lead in db.query(models.Lead).all()}
    leads = db.query(models.Lead).order_by(models.Lead.name).all()
    return templates.TemplateResponse(
        request, "campaigns.html",
        _ctx(request, user, campaigns=campaigns,
             enrolled_by_campaign=enrolled_by_campaign,
             lead_names=lead_names, leads=leads))


@router.get("/copilot", response_class=HTMLResponse)
def copilot_page(request: Request, db: Session = Depends(get_db)):
    user, redir = _page_user(request, db)
    if redir:
        return redir
    return templates.TemplateResponse(request, "copilot.html", _ctx(request, user))


@router.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, db: Session = Depends(get_db)):
    user, redir = _page_user(request, db)
    if redir:
        return redir
    return templates.TemplateResponse(
        request, "channels.html",
        _ctx(request, user, adapters=adapter_statuses()))


# ---------------------------------------------------------------- APIs ---

@router.get("/api/dashboard/stats")
def dashboard_stats(
    range: str = Query("7d", pattern="^(24h|7d|30d)$"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}[range]
    since = _now() - timedelta(hours=hours)
    leads = db.query(models.Lead).filter(models.Lead.created_at >= since).all()
    intent_counts = {"Hot": 0, "Warm": 0, "Cold": 0, "Unqualified": 0,
                     "Unclassified": 0}
    outcome_counts = {"Won": 0, "Lost": 0, "In Pipeline": 0}
    for lead in leads:
        intent_counts[lead.intent if lead.intent in intent_counts else "Unclassified"] += 1
        if lead.stage in ("Won", "Lost"):
            outcome_counts[lead.stage] += 1
        else:
            outcome_counts["In Pipeline"] += 1
    return {
        "range": range,
        "total_leads": len(leads),
        "intent_counts": intent_counts,
        "outcome_counts": outcome_counts,
    }


class LeadIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    source: str = "manual"


@router.post("/api/leads", status_code=201)
def create_lead(
    payload: LeadIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    lead = models.Lead(
        name=payload.name.strip(), email=payload.email.strip(),
        phone=payload.phone.strip(), company=payload.company.strip(),
        source=payload.source.strip() or "manual",
    )
    db.add(lead)
    db.flush()
    _log(db, lead.id, "lead_created", {"source": lead.source})
    db.commit()
    return _serialize_lead(lead)


@router.post("/api/leads/import-csv")
def import_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    raw = file.file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    imported = 0
    for row in reader:
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip()
        if not name and not email:
            continue
        lead = models.Lead(
            name=name, email=email,
            phone=(row.get("phone") or "").strip(),
            company=(row.get("company") or "").strip(),
            source="csv",
        )
        db.add(lead)
        db.flush()
        _log(db, lead.id, "lead_created", {"source": "csv"})
        imported += 1
    db.commit()
    return {"imported": imported}


class StageIn(BaseModel):
    stage: str


@router.post("/api/leads/{lead_id}/stage")
def set_stage(
    lead_id: int,
    payload: StageIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if payload.stage not in agent_logic.KANBAN_STAGES:
        raise HTTPException(status_code=400, detail="Invalid stage")
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    old = lead.stage
    lead.stage = payload.stage
    _log(db, lead.id, "stage_changed", {"from": old, "to": payload.stage})
    db.commit()
    return _serialize_lead(lead)


class MessageIn(BaseModel):
    text: str
    channel: str = "simulator"


@router.post("/api/leads/{lead_id}/messages")
def inbound_message(
    lead_id: int,
    payload: MessageIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    db.add(models.Message(lead_id=lead.id, role="lead", text=text,
                          channel=payload.channel))
    db.flush()
    history = [
        m.text for m in db.query(models.Message)
        .filter(models.Message.lead_id == lead.id, models.Message.role == "lead")
        .order_by(models.Message.id).all()
    ][:-1]
    result = agent_logic.process_inbound(lead, text, get_provider(), history)
    try:
        slots = json.loads(lead.slots_json or "{}")
    except json.JSONDecodeError:
        slots = {}
    slots.update(result.slot_updates)
    lead.slots_json = json.dumps(slots)
    if result.kanban_stage:
        lead.stage = result.kanban_stage
    if result.intent:
        label, conf, reasons = result.intent
        lead.intent = label
        lead.intent_confidence = conf
        lead.intent_reasons_json = json.dumps(reasons)
    for kind, detail in result.events:
        _log(db, lead.id, kind, detail)
    adapter = get_adapter("simulator")
    adapter.send(db, lead, result.reply)
    _log(db, lead.id, "agent_replied",
         {"channel": adapter.name, "agent_stage": result.agent_stage})
    db.commit()
    return {
        "reply": result.reply,
        "agent_stage": result.agent_stage,
        "lead": _serialize_lead(lead),
    }


class WebhookLeadIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    source: str = "webhook"


@router.post("/api/webhooks/leads", status_code=201)
def webhook_lead(payload: WebhookLeadIn, request: Request,
                 db: Session = Depends(get_db)):
    key = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(key, WEBHOOK_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid API key")
    lead = models.Lead(
        name=payload.name.strip(), email=payload.email.strip(),
        phone=payload.phone.strip(), company=payload.company.strip(),
        source=payload.source.strip() or "webhook",
    )
    db.add(lead)
    db.flush()
    _log(db, lead.id, "webhook_received", {"source": lead.source})
    db.commit()
    return _serialize_lead(lead)


# ------------------------------------------------------------- calendar ---

@router.get("/api/calendar/slots")
def api_slots(
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).date()
    return {"date": date, "slots": calendar_util.free_slots(day, db)}


class AppointmentIn(BaseModel):
    lead_id: int
    starts_at: str  # ISO datetime


@router.post("/api/appointments", status_code=201)
def create_appointment(
    payload: AppointmentIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    lead = db.query(models.Lead).filter(models.Lead.id == payload.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        starts_at = datetime.fromisoformat(payload.starts_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid starts_at")
    title = f"Intro call with {lead.name or lead.email}".strip()
    try:
        appt = calendar_util.book_appointment(
            db, lead.id, title, starts_at,
            attendee_email=lead.email)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    lead.stage = "Booked"
    _log(db, lead.id, "appointment_booked", {"appointment_id": appt.id})
    db.commit()
    return {
        "id": appt.id,
        "title": appt.title,
        "starts_at": appt.starts_at.isoformat(),
        "ends_at": appt.ends_at.isoformat(),
        "ics_uid": appt.ics_uid,
    }


@router.get("/api/appointments/{appt_id}/ics")
def appointment_ics(
    appt_id: int, db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    appt = db.query(models.Appointment).filter(
        models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    body = calendar_util.ics_for_appointment(appt, organizer=user.email)
    return Response(
        content=body, media_type="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=appointment-{appt.id}.ics"},
    )


@router.get("/api/calendar/feed/{token}.ics")
def calendar_feed(token: str, db: Session = Depends(get_db)):
    feed = db.query(models.FeedToken).filter(models.FeedToken.token == token).first()
    if not feed:
        raise HTTPException(status_code=404, detail="Unknown feed token")
    owner = db.query(models.User).filter(models.User.id == feed.user_id).first()
    body = calendar_util.feed_ics(db, organizer=owner.email if owner else "")
    return PlainTextResponse(body, media_type="text/calendar")


# ------------------------------------------------------------ campaigns ---

class CampaignIn(BaseModel):
    name: str
    template_text: str
    followup_hours: int = 24


@router.post("/api/campaigns", status_code=201)
def create_campaign(
    payload: CampaignIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if not payload.name.strip() or not payload.template_text.strip():
        raise HTTPException(status_code=400, detail="Name and template required")
    campaign = models.Campaign(
        name=payload.name.strip(),
        template_text=payload.template_text.strip(),
        followup_hours=max(1, payload.followup_hours),
        active=True,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return {"id": campaign.id, "name": campaign.name}


class EnrollIn(BaseModel):
    lead_ids: list[int]


@router.post("/api/campaigns/{campaign_id}/enroll")
def enroll_leads(
    campaign_id: int,
    payload: EnrollIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    enrolled = 0
    for lead_id in payload.lead_ids:
        lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
        if not lead:
            continue
        exists = db.query(models.Enrollment).filter(
            models.Enrollment.campaign_id == campaign_id,
            models.Enrollment.lead_id == lead_id,
            models.Enrollment.status == "active").first()
        if exists:
            continue
        db.add(models.Enrollment(
            campaign_id=campaign_id, lead_id=lead_id,
            last_agent_msg_at=_now(), status="active"))
        _log(db, lead_id, "campaign_enrolled", {"campaign_id": campaign_id})
        enrolled += 1
    db.commit()
    return {"enrolled": enrolled}


@router.post("/api/campaigns/{campaign_id}/run-now")
def campaign_run_now(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    sent = run_due_followups(db, campaign_id=campaign_id)
    return {"sent": sent, "count": len(sent)}


# -------------------------------------------------------------- copilot ---

class CopilotIn(BaseModel):
    question: str


@router.post("/api/copilot")
def copilot_ask(
    payload: CopilotIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="Empty question")
    return copilot.answer_question(db, payload.question)


@router.get("/api/channels")
def channels_api(user: models.User = Depends(get_current_user)):
    return {"adapters": adapter_statuses()}
