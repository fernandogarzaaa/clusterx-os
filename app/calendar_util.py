"""Calendar: availability slots, conflict-checked booking, ICS output.

ICS is built by hand (no new dependencies). All times are stored and
emitted as UTC.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from app import models

SLOT_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def free_slots(day: date, db: Session) -> list[dict]:
    """Free 30-minute slots for a date from SlotTemplate minus appointments."""
    weekday = day.weekday()  # 0=Monday
    templates = (
        db.query(models.SlotTemplate)
        .filter(models.SlotTemplate.weekday == weekday)
        .all()
    )
    if not templates:
        return []
    day_start = datetime.combine(day, time.min)
    day_end = datetime.combine(day, time.max)
    busy = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.starts_at < day_end,
            models.Appointment.ends_at > day_start,
        )
        .all()
    )
    busy_ranges = [(a.starts_at, a.ends_at) for a in busy]
    slots: list[dict] = []
    for tpl in templates:
        cursor = datetime.combine(day, _parse_hhmm(tpl.start_time))
        end = datetime.combine(day, _parse_hhmm(tpl.end_time))
        while cursor + timedelta(minutes=SLOT_MINUTES) <= end:
            slot_end = cursor + timedelta(minutes=SLOT_MINUTES)
            overlaps = any(
                cursor < b_end and slot_end > b_start for b_start, b_end in busy_ranges
            )
            if not overlaps and cursor > _now():
                slots.append(
                    {"starts_at": cursor.isoformat(), "ends_at": slot_end.isoformat()}
                )
            cursor = slot_end
    return slots


def book_appointment(
    db: Session,
    lead_id: int,
    title: str,
    starts_at: datetime,
    ends_at: datetime | None = None,
    attendee_email: str = "",
) -> models.Appointment:
    """Book an appointment with conflict checking. Raises ValueError on clash."""
    starts_at = starts_at.replace(tzinfo=None)
    ends_at = (ends_at.replace(tzinfo=None) if ends_at
               else starts_at + timedelta(minutes=SLOT_MINUTES))
    if starts_at < _now() - timedelta(minutes=1):
        raise ValueError("Cannot book an appointment in the past.")
    clash = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.starts_at < ends_at,
            models.Appointment.ends_at > starts_at,
        )
        .first()
    )
    if clash:
        raise ValueError(
            f"Conflicts with existing booking '{clash.title}' "
            f"at {clash.starts_at.isoformat()}."
        )
    appt = models.Appointment(
        lead_id=lead_id,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        attendee_email=attendee_email,
        ics_uid=f"{uuid.uuid4().hex}@clusterx-os",
    )
    db.add(appt)
    db.flush()
    return appt


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace(
        "\n", "\\n"
    )


def _ics_dt(value: datetime) -> str:
    return value.replace(tzinfo=None).strftime("%Y%m%dT%H%M%SZ")


def ics_for_appointment(appt: models.Appointment, organizer: str = "") -> str:
    stamp = _ics_dt(_now())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ClusterX OS//Lead Calendar//EN",
        "BEGIN:VEVENT",
        f"UID:{appt.ics_uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_ics_dt(appt.starts_at)}",
        f"DTEND:{_ics_dt(appt.ends_at)}",
        f"SUMMARY:{_ics_escape(appt.title)}",
    ]
    if appt.attendee_email:
        lines.append(f"ATTENDEE;CN={_ics_escape(appt.attendee_email)}:mailto:{appt.attendee_email}")
    if organizer:
        lines.append(f"ORGANIZER:mailto:{organizer}")
    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def feed_ics(db: Session, organizer: str = "") -> str:
    """Full VCALENDAR feed of all appointments (gated by feed token at the route)."""
    appts = db.query(models.Appointment).order_by(models.Appointment.starts_at).all()
    events: list[str] = []
    for appt in appts:
        body = ics_for_appointment(appt, organizer=organizer)
        inner = body.split("BEGIN:VEVENT", 1)[1].rsplit("END:VCALENDAR", 1)[0]
        events.append("BEGIN:VEVENT" + inner.rstrip("\r\n"))
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "PRODID:-//ClusterX OS//Lead Calendar Feed//EN\r\n"
        + "\r\n".join(events)
        + ("\r\n" if events else "")
        + "END:VCALENDAR\r\n"
    )
