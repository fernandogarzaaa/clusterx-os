"""Scheduling: availability, conflict-checked booking, cancellation, ICS.

Availability templates are interpreted in BUSINESS_TIMEZONE; everything is
stored as naive UTC. An external calendar adapter (Google / Cal.com), when
configured, filters availability and receives the event.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clusterx_os.integrations import IntegrationError
from clusterx_os.integrations.calendars import CalendarAdapter, EventRequest, Interval, overlaps
from clusterx_os.models import Appointment, Lead, SlotTemplate, utcnow

logger = logging.getLogger(__name__)


class BookingError(Exception):
    """A booking could not be made (conflict, past time, upstream failure)."""


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def to_utc_naive(local: datetime) -> datetime:
    return local.astimezone(UTC).replace(tzinfo=None)


def to_local(utc_naive: datetime, tz: ZoneInfo) -> datetime:
    return utc_naive.replace(tzinfo=UTC).astimezone(tz)


def slot_label(utc_naive: datetime, tz: ZoneInfo) -> str:
    """Human label in the business timezone, e.g. 'Tue 14 Oct, 10:00 AM (Asia/Manila)'."""
    local = to_local(utc_naive, tz)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{local:%a} {local.day} {local:%b}, {hour}:{local:%M} {meridiem} ({tz.key})"


def busy_intervals(db: Session, start: datetime, end: datetime) -> list[Interval]:
    rows = db.execute(
        select(Appointment.starts_at, Appointment.ends_at).where(
            Appointment.status == "confirmed",
            Appointment.starts_at < end,
            Appointment.ends_at > start,
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


def template_candidates(db: Session, day: date, tz: ZoneInfo, slot_minutes: int) -> list[Interval]:
    """Every slot the availability templates offer on a local calendar day."""
    templates = db.scalars(select(SlotTemplate).where(SlotTemplate.weekday == day.weekday())).all()
    step = timedelta(minutes=slot_minutes)
    out: list[Interval] = []
    for tpl in sorted(templates, key=lambda t: t.start_time):
        cursor = datetime.combine(day, parse_hhmm(tpl.start_time), tzinfo=tz)
        end = datetime.combine(day, parse_hhmm(tpl.end_time), tzinfo=tz)
        while cursor + step <= end:
            out.append((to_utc_naive(cursor), to_utc_naive(cursor + step)))
            cursor += step
    return out


def open_slots(
    db: Session,
    days: list[date],
    tz: ZoneInfo,
    slot_minutes: int,
    adapter: CalendarAdapter | None = None,
    now: datetime | None = None,
) -> list[Interval]:
    """Free slots across local days: templates minus bookings, past, and upstream busy."""
    now = now or utcnow()
    candidates: list[Interval] = []
    for day in days:
        candidates += template_candidates(db, day, tz, slot_minutes)
    candidates = sorted(c for c in candidates if c[0] > now)
    if not candidates:
        return []
    busy = busy_intervals(db, candidates[0][0], candidates[-1][1])
    candidates = [c for c in candidates if not any(overlaps(c, b) for b in busy)]
    if adapter is not None and candidates:
        candidates = adapter.filter_available(candidates)
    return candidates


def next_open_slots(
    db: Session,
    tz: ZoneInfo,
    slot_minutes: int,
    horizon_days: int,
    count: int = 3,
    adapter: CalendarAdapter | None = None,
    after: datetime | None = None,
    now: datetime | None = None,
) -> list[Interval]:
    now = now or utcnow()
    start = max(after or now, now)
    first_day = to_local(start, tz).date()
    days = [first_day + timedelta(days=i) for i in range(horizon_days + 1)]
    slots = open_slots(db, days, tz, slot_minutes, adapter, now)
    return [s for s in slots if s[0] >= start][:count]


def is_template_slot(db: Session, starts_at: datetime, tz: ZoneInfo, slot_minutes: int) -> bool:
    day = to_local(starts_at, tz).date()
    return any(c[0] == starts_at for c in template_candidates(db, day, tz, slot_minutes))


def book(
    db: Session,
    lead: Lead,
    starts_at: datetime,
    *,
    slot_minutes: int,
    adapter: CalendarAdapter | None = None,
    title: str | None = None,
    now: datetime | None = None,
) -> Appointment:
    """Reserve a slot and create the upstream event. Raises BookingError.

    Runs inside a SAVEPOINT, so a failed booking leaves the caller's other
    pending changes intact. The caller commits (see ``commit_booking``).
    """
    now = now or utcnow()
    if starts_at.tzinfo is not None:
        starts_at = starts_at.astimezone(UTC).replace(tzinfo=None)
    starts_at = starts_at.replace(second=0, microsecond=0)
    ends_at = starts_at + timedelta(minutes=slot_minutes)
    if starts_at < now:
        raise BookingError("Cannot book an appointment in the past.")
    clash = busy_intervals(db, starts_at, ends_at)
    if clash:
        raise BookingError(f"Conflicts with an existing booking at {clash[0][0].isoformat()}Z.")
    if adapter is not None and not adapter.filter_available([(starts_at, ends_at)]):
        raise BookingError("That time is not available on the connected calendar.")

    company = f" ({lead.company})" if lead.company else ""
    appt = Appointment(
        lead_id=lead.id,
        title=title or f"Intro call with {lead.name or lead.email or 'lead'}{company}",
        starts_at=starts_at,
        ends_at=ends_at,
        attendee_email=lead.email,
        status="confirmed",
        provider=adapter.name if adapter else "local",
        ics_uid=f"{uuid.uuid4().hex}@clusterx-os",
    )
    savepoint = db.begin_nested()
    db.add(appt)
    try:
        db.flush()
    except IntegrityError as exc:
        savepoint.rollback()
        raise BookingError("That time was just booked by someone else.") from exc
    if adapter is not None:
        try:
            appt.external_id = adapter.create_event(EventRequest(
                title=appt.title, starts_at=starts_at, ends_at=ends_at,
                attendee_name=lead.name, attendee_email=lead.email, attendee_phone=lead.phone,
                description="Booked by the ClusterX OS qualification agent.",
            ))
        except IntegrationError as exc:
            savepoint.rollback()
            logger.warning("upstream booking failed",
                           extra={"provider": adapter.name, "error": str(exc)})
            raise BookingError(f"The calendar provider rejected the booking: {exc}") from exc
    savepoint.commit()
    return appt


def commit_booking(db: Session, appt: Appointment, adapter: CalendarAdapter | None) -> None:
    """Commit; if the commit fails, cancel the upstream event so nothing is orphaned."""
    try:
        db.commit()
    except Exception:
        db.rollback()
        if adapter is not None and appt.external_id:
            try:
                adapter.cancel_event(appt.external_id)
            except IntegrationError:
                logger.error("could not roll back upstream event",
                             extra={"external_id": appt.external_id})
        raise


def cancel(db: Session, appt: Appointment, adapter: CalendarAdapter | None = None) -> Appointment:
    """Cancel upstream (if it lives there) and locally. Does not commit."""
    if appt.status == "cancelled":
        return appt
    if appt.external_id and adapter is not None and appt.provider == adapter.name:
        try:
            adapter.cancel_event(appt.external_id)
        except IntegrationError as exc:
            raise BookingError(f"The calendar provider could not cancel the event: {exc}") from exc
    appt.status = "cancelled"
    appt.cancelled_at = utcnow()
    return appt


# ------------------------------------------------------------------ ICS ---

def ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_dt(value: datetime) -> str:
    return value.replace(tzinfo=None).strftime("%Y%m%dT%H%M%SZ")


def fold_line(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: list[str] = []
    current = b""
    limit = 75
    for ch in line:
        enc = ch.encode("utf-8")
        if len(current) + len(enc) > limit:
            parts.append(current.decode("utf-8"))
            current = b""
            limit = 74  # continuation lines start with a space
        current += enc
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _vevent(appt: Appointment, organizer: str, stamp: datetime) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{appt.ics_uid}",
        f"DTSTAMP:{_ics_dt(stamp)}",
        f"DTSTART:{_ics_dt(appt.starts_at)}",
        f"DTEND:{_ics_dt(appt.ends_at)}",
        f"SUMMARY:{ics_escape(appt.title)}",
    ]
    if appt.attendee_email:
        lines.append(f"ATTENDEE;CN={ics_escape(appt.attendee_email)}:mailto:{appt.attendee_email}")
    if organizer:
        lines.append(f"ORGANIZER:mailto:{organizer}")
    status = "CANCELLED" if appt.status == "cancelled" else "CONFIRMED"
    lines += [f"STATUS:{status}", "END:VEVENT"]
    return lines


def calendar_ics(appts: list[Appointment], organizer: str = "", name: str = "ClusterX OS",
                 now: datetime | None = None) -> str:
    stamp = now or utcnow()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ClusterX OS//Lead Calendar//EN",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{ics_escape(name)}"]
    for appt in appts:
        lines += _vevent(appt, organizer, stamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"
