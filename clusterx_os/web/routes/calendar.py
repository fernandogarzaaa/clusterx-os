"""Calendar API: availability, slots, appointments, ICS downloads and feed."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from clusterx_os import booking
from clusterx_os.config import Settings
from clusterx_os.db import get_db
from clusterx_os.integrations import IntegrationError
from clusterx_os.models import Appointment, FeedToken, SlotTemplate, User, utcnow
from clusterx_os.services import leads as lead_service
from clusterx_os.services.outbox import enqueue_crm_note
from clusterx_os.web.common import get_lead_or_404
from clusterx_os.web.deps import (
    Integrations,
    current_user,
    integrations_dep,
    require_admin,
    settings_dep,
)
from clusterx_os.web.schemas import AppointmentIn, SlotTemplateIn

router = APIRouter()


def serialize_appointment(a: Appointment) -> dict[str, Any]:
    return {"id": a.id, "lead_id": a.lead_id, "title": a.title,
            "starts_at": a.starts_at.isoformat() + "Z", "ends_at": a.ends_at.isoformat() + "Z",
            "status": a.status, "provider": a.provider, "external_id": a.external_id,
            "attendee_email": a.attendee_email, "ics_uid": a.ics_uid}


def parse_start(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422,
                            detail="starts_at must be an ISO-8601 datetime") from exc
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _availability(db: Session, settings: Settings) -> dict[str, Any]:
    rows = db.scalars(select(SlotTemplate).order_by(SlotTemplate.weekday,
                                                     SlotTemplate.start_time)).all()
    return {"timezone": settings.BUSINESS_TIMEZONE, "slot_minutes": settings.SLOT_MINUTES,
            "windows": [{"weekday": r.weekday, "start_time": r.start_time,
                         "end_time": r.end_time} for r in rows]}


@router.get("/api/calendar/availability")
def get_availability(db: Session = Depends(get_db), settings: Settings = Depends(settings_dep),
                     _user: User = Depends(current_user)) -> dict[str, Any]:
    return _availability(db, settings)


@router.put("/api/calendar/availability")
def put_availability(windows: list[SlotTemplateIn], db: Session = Depends(get_db),
                     settings: Settings = Depends(settings_dep),
                     _admin: User = Depends(require_admin)) -> dict[str, Any]:
    """Replace the weekly availability windows (times in BUSINESS_TIMEZONE)."""
    for w in windows:
        if w.end_time <= w.start_time:
            raise HTTPException(status_code=422, detail="end_time must be after start_time")
    db.execute(delete(SlotTemplate))
    for w in windows:
        db.add(SlotTemplate(weekday=w.weekday, start_time=w.start_time, end_time=w.end_time))
    db.commit()
    return _availability(db, settings)


@router.get("/api/calendar/slots")
def api_slots(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
              db: Session = Depends(get_db), settings: Settings = Depends(settings_dep),
              integrations: Integrations = Depends(integrations_dep),
              _user: User = Depends(current_user)) -> dict[str, Any]:
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date") from exc
    try:
        slots = booking.open_slots(db, [day], settings.tz, settings.SLOT_MINUTES,
                                   integrations.calendar)
    except IntegrationError as exc:
        raise HTTPException(status_code=502, detail=f"Calendar provider error: {exc}") from exc
    return {"date": date, "timezone": settings.BUSINESS_TIMEZONE,
            "slots": [{"starts_at": s.isoformat() + "Z", "ends_at": e.isoformat() + "Z",
                       "label": booking.slot_label(s, settings.tz)} for s, e in slots]}


@router.get("/api/appointments")
def list_appointments(upcoming: bool = True, db: Session = Depends(get_db),
                      _user: User = Depends(current_user)) -> dict[str, Any]:
    query = select(Appointment).order_by(Appointment.starts_at)
    if upcoming:
        query = query.where(Appointment.starts_at >= utcnow(), Appointment.status == "confirmed")
    return {"appointments": [serialize_appointment(a) for a in db.scalars(query.limit(500))]}


@router.post("/api/appointments", status_code=201)
def create_appointment(payload: AppointmentIn, db: Session = Depends(get_db),
                       settings: Settings = Depends(settings_dep),
                       integrations: Integrations = Depends(integrations_dep),
                       user: User = Depends(current_user)) -> dict[str, Any]:
    lead = get_lead_or_404(db, payload.lead_id)
    try:
        appt = booking.book(db, lead, parse_start(payload.starts_at),
                            slot_minutes=settings.SLOT_MINUTES, adapter=integrations.calendar)
    except booking.BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status_code=502, detail=f"Calendar provider error: {exc}") from exc
    lead_service.log_event(db, lead.id, "appointment_booked",
                           {"appointment_id": appt.id, "by": user.email})
    lead_service.set_stage(db, settings, lead, "Booked", by=user.email)
    enqueue_crm_note(db, settings, lead, f"Intro call booked for {appt.starts_at.isoformat()}Z.")
    booking.commit_booking(db, appt, integrations.calendar)
    return serialize_appointment(appt)


@router.post("/api/appointments/{appt_id}/cancel")
def cancel_appointment(appt_id: int, db: Session = Depends(get_db),
                       integrations: Integrations = Depends(integrations_dep),
                       user: User = Depends(current_user)) -> dict[str, Any]:
    appt = db.get(Appointment, appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    try:
        booking.cancel(db, appt, integrations.calendar)
    except booking.BookingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    lead_service.log_event(db, appt.lead_id, "appointment_cancelled",
                           {"appointment_id": appt.id, "by": user.email})
    db.commit()
    return serialize_appointment(appt)


@router.get("/api/appointments/{appt_id}/ics")
def appointment_ics(appt_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> Response:
    appt = db.get(Appointment, appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return Response(
        content=booking.calendar_ics([appt], organizer=user.email),
        media_type="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=appointment-{appt.id}.ics"},
    )


@router.get("/api/calendar/feed/{token}.ics")
def calendar_feed(token: str, db: Session = Depends(get_db)) -> Response:
    feed = db.scalar(select(FeedToken).where(FeedToken.token == token))
    owner = db.get(User, feed.user_id) if feed is not None else None
    if owner is None or not owner.is_active:
        raise HTTPException(status_code=404, detail="Unknown feed token")
    appts = list(db.scalars(select(Appointment).order_by(Appointment.starts_at)))
    return Response(booking.calendar_ics(appts, organizer=owner.email), media_type="text/calendar")


@router.post("/api/calendar/feed-token")
def rotate_feed_token(db: Session = Depends(get_db),
                      user: User = Depends(current_user)) -> dict[str, str]:
    db.execute(delete(FeedToken).where(FeedToken.user_id == user.id))
    token = secrets.token_urlsafe(32)
    db.add(FeedToken(user_id=user.id, token=token))
    db.commit()
    return {"token": token, "url": f"/api/calendar/feed/{token}.ics"}
