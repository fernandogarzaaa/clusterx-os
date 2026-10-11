"""Server-rendered dashboard pages (cookie session; redirect to /login)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from clusterx_os.booking import to_local
from clusterx_os.config import Settings
from clusterx_os.db import get_db
from clusterx_os.integrations.calendars import calendar_status
from clusterx_os.integrations.channels import adapter_statuses
from clusterx_os.integrations.crm import crm_status
from clusterx_os.models import (
    KANBAN_STAGES,
    Appointment,
    Campaign,
    Enrollment,
    Event,
    FeedToken,
    Lead,
    Message,
    User,
    utcnow,
)
from clusterx_os.web.common import templates
from clusterx_os.web.deps import optional_user, settings_dep

router = APIRouter(include_in_schema=False)


def _render(request: Request, name: str, user: User, settings: Settings,
            **extra: Any) -> Response:
    ctx = {"user": user, "app_name": settings.APP_NAME,
           "business_tz": settings.BUSINESS_TIMEZONE, **extra}
    return templates.TemplateResponse(request, name, ctx)


def _login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, range: str = Query("7d", pattern="^(24h|7d|30d)$"),  # noqa: A002
              user: User | None = Depends(optional_user),
              settings: Settings = Depends(settings_dep)) -> Response:
    if user is None:
        return _login()
    return _render(request, "dashboard.html", user, settings, range=range)


@router.get("/leads", response_class=HTMLResponse)
def leads_board(request: Request, db: Session = Depends(get_db),
                user: User | None = Depends(optional_user),
                settings: Settings = Depends(settings_dep)) -> Response:
    if user is None:
        return _login()
    board: dict[str, list[Lead]] = {s: [] for s in KANBAN_STAGES}
    for lead in db.scalars(select(Lead).order_by(Lead.created_at.desc()).limit(1000)):
        board.setdefault(lead.stage, []).append(lead)
    return _render(request, "leads.html", user, settings, board=board, stages=KANBAN_STAGES)


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_db),
                user: User | None = Depends(optional_user),
                settings: Settings = Depends(settings_dep)) -> Response:
    if user is None:
        return _login()
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    messages = db.scalars(select(Message).where(Message.lead_id == lead_id)
                          .order_by(Message.id)).all()
    events = db.scalars(select(Event).where(Event.lead_id == lead_id)
                        .order_by(Event.id.desc()).limit(50)).all()
    appointments = db.scalars(select(Appointment).where(Appointment.lead_id == lead_id)
                              .order_by(Appointment.starts_at)).all()
    return _render(request, "lead_detail.html", user, settings, lead=lead, messages=messages,
                   events=events, appointments=appointments,
                   reasons=json.loads(lead.intent_reasons_json or "[]"), stages=KANBAN_STAGES)


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, db: Session = Depends(get_db),
                  date: str = Query(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
                  user: User | None = Depends(optional_user),
                  settings: Settings = Depends(settings_dep)) -> Response:
    if user is None:
        return _login()
    day = date or to_local(utcnow(), settings.tz).date().isoformat()
    bookings = db.scalars(select(Appointment).where(
        Appointment.starts_at >= utcnow(), Appointment.status == "confirmed")
        .order_by(Appointment.starts_at).limit(50)).all()
    leads = db.scalars(select(Lead).order_by(Lead.name).limit(1000)).all()
    token = db.scalar(select(FeedToken).where(FeedToken.user_id == user.id))
    return _render(request, "calendar.html", user, settings, day=day, bookings=bookings,
                   leads=leads, feed_token=token.token if token else "",
                   calendar=calendar_status(settings))


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, db: Session = Depends(get_db),
                   user: User | None = Depends(optional_user),
                   settings: Settings = Depends(settings_dep)) -> Response:
    if user is None:
        return _login()
    campaigns = db.scalars(select(Campaign).order_by(Campaign.id)).all()
    enrolled: dict[int, list[Enrollment]] = {}
    for enr in db.scalars(select(Enrollment)):
        enrolled.setdefault(enr.campaign_id, []).append(enr)
    leads = db.scalars(select(Lead).order_by(Lead.name).limit(1000)).all()
    return _render(request, "campaigns.html", user, settings, campaigns=campaigns,
                   enrolled_by_campaign=enrolled,
                   lead_names={lead.id: lead.name for lead in leads}, leads=leads)


@router.get("/copilot", response_class=HTMLResponse)
def copilot_page(request: Request, user: User | None = Depends(optional_user),
                 settings: Settings = Depends(settings_dep)) -> Response:
    if user is None:
        return _login()
    return _render(request, "copilot.html", user, settings)


@router.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, user: User | None = Depends(optional_user),
                  settings: Settings = Depends(settings_dep)) -> Response:
    if user is None:
        return _login()
    return _render(request, "channels.html", user, settings,
                   adapters=adapter_statuses(settings), crm=crm_status(settings),
                   calendar=calendar_status(settings), llm_provider=settings.LLM_PROVIDER)
