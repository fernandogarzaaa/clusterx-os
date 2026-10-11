from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from clusterx_os import booking
from clusterx_os.integrations import IntegrationError
from clusterx_os.integrations.calendars import EventRequest, Interval
from clusterx_os.models import Appointment, Lead, SlotTemplate

UTC_TZ = ZoneInfo("UTC")
MANILA = ZoneInfo("Asia/Manila")
# Wednesday 14 Oct 2026 08:00 UTC
NOW = datetime(2026, 10, 14, 8, 0)
WED = date(2026, 10, 14)


class FakeCalendar:
    name = "fake"

    def __init__(self, busy: list[Interval] | None = None, fail_create: bool = False,
                 fail_cancel: bool = False) -> None:
        self.busy = busy or []
        self.fail_create = fail_create
        self.fail_cancel = fail_cancel
        self.created: list[EventRequest] = []
        self.cancelled: list[str] = []

    def filter_available(self, candidates: list[Interval]) -> list[Interval]:
        return [c for c in candidates
                if not any(c[0] < b[1] and b[0] < c[1] for b in self.busy)]

    def create_event(self, event: EventRequest) -> str:
        if self.fail_create:
            raise IntegrationError("boom", retryable=True)
        self.created.append(event)
        return f"ext-{len(self.created)}"

    def cancel_event(self, external_id: str) -> None:
        if self.fail_cancel:
            raise IntegrationError("cannot cancel")
        self.cancelled.append(external_id)

    def status(self) -> dict[str, Any]:
        return {"name": self.name}


def _lead(db: Session, **kw: Any) -> Lead:
    lead = Lead(name=kw.get("name", "Ann Lee"), email=kw.get("email", "ann@x.example"),
                phone="+15550001111", company=kw.get("company", "Acme"))
    db.add(lead)
    db.commit()
    return lead


def _window(db: Session, weekday: int, start: str, end: str) -> None:
    db.execute(delete(SlotTemplate))
    db.add(SlotTemplate(weekday=weekday, start_time=start, end_time=end))
    db.commit()


def test_parse_hhmm_and_conversions() -> None:
    assert booking.parse_hhmm("09:30") == time(9, 30)
    local = datetime(2026, 10, 14, 9, 0, tzinfo=MANILA)
    assert booking.to_utc_naive(local) == datetime(2026, 10, 14, 1, 0)
    assert booking.to_local(datetime(2026, 10, 14, 1, 0), MANILA) == local


def test_slot_label() -> None:
    assert booking.slot_label(datetime(2026, 10, 14, 1, 0), MANILA) == \
        "Wed 14 Oct, 9:00 AM (Asia/Manila)"
    assert booking.slot_label(datetime(2026, 10, 14, 12, 5), UTC_TZ) == "Wed 14 Oct, 12:05 PM (UTC)"
    assert booking.slot_label(datetime(2026, 10, 14, 0, 30), UTC_TZ) == "Wed 14 Oct, 12:30 AM (UTC)"
    assert booking.slot_label(datetime(2026, 10, 14, 23, 0), UTC_TZ) == "Wed 14 Oct, 11:00 PM (UTC)"


def test_template_candidates_step_and_boundary(db: Session) -> None:
    _window(db, 2, "09:00", "10:30")
    slots = booking.template_candidates(db, WED, UTC_TZ, 30)
    assert slots == [
        (datetime(2026, 10, 14, 9, 0), datetime(2026, 10, 14, 9, 30)),
        (datetime(2026, 10, 14, 9, 30), datetime(2026, 10, 14, 10, 0)),
        (datetime(2026, 10, 14, 10, 0), datetime(2026, 10, 14, 10, 30)),
    ]
    # a partial trailing slot is not offered
    assert len(booking.template_candidates(db, WED, UTC_TZ, 60)) == 1
    assert booking.template_candidates(db, WED + timedelta(days=1), UTC_TZ, 30) == []


def test_template_candidates_in_business_timezone(db: Session) -> None:
    _window(db, 2, "09:00", "09:30")
    assert booking.template_candidates(db, WED, MANILA, 30) == [
        (datetime(2026, 10, 14, 1, 0), datetime(2026, 10, 14, 1, 30))
    ]


def test_template_candidates_multiple_windows_sorted(db: Session) -> None:
    db.execute(delete(SlotTemplate))
    db.add(SlotTemplate(weekday=2, start_time="14:00", end_time="14:30"))
    db.add(SlotTemplate(weekday=2, start_time="09:00", end_time="09:30"))
    db.commit()
    starts = [s for s, _ in booking.template_candidates(db, WED, UTC_TZ, 30)]
    assert starts == [datetime(2026, 10, 14, 9, 0), datetime(2026, 10, 14, 14, 0)]


def test_open_slots_excludes_past_and_booked(db: Session) -> None:
    _window(db, 2, "07:00", "10:00")
    lead = _lead(db)
    booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    db.commit()
    slots = booking.open_slots(db, [WED], UTC_TZ, 30, now=NOW)
    assert [s for s, _ in slots] == [datetime(2026, 10, 14, 8, 30), datetime(2026, 10, 14, 9, 30)]


def test_open_slots_cancelled_booking_frees_slot(db: Session) -> None:
    _window(db, 2, "09:00", "10:00")
    lead = _lead(db)
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    booking.cancel(db, appt)
    db.commit()
    assert len(booking.open_slots(db, [WED], UTC_TZ, 30, now=NOW)) == 2


def test_open_slots_uses_adapter(db: Session) -> None:
    _window(db, 2, "09:00", "10:00")
    cal = FakeCalendar(busy=[(datetime(2026, 10, 14, 9, 15), datetime(2026, 10, 14, 9, 45))])
    assert booking.open_slots(db, [WED], UTC_TZ, 30, cal, now=NOW) == []
    assert booking.open_slots(db, [WED + timedelta(days=1)], UTC_TZ, 30, cal, now=NOW) == []


def test_next_open_slots_count_and_after(db: Session) -> None:
    db.execute(delete(SlotTemplate))
    for wd in range(5):
        db.add(SlotTemplate(weekday=wd, start_time="09:00", end_time="11:00"))
    db.commit()
    slots = booking.next_open_slots(db, UTC_TZ, 30, 14, count=3, now=NOW)
    assert [s for s, _ in slots] == [datetime(2026, 10, 14, 9, 0), datetime(2026, 10, 14, 9, 30),
                                     datetime(2026, 10, 14, 10, 0)]
    after = booking.next_open_slots(db, UTC_TZ, 30, 14, count=2,
                                    after=datetime(2026, 10, 14, 10, 15), now=NOW)
    assert [s for s, _ in after] == [datetime(2026, 10, 14, 10, 30), datetime(2026, 10, 15, 9, 0)]
    # weekend is skipped: Friday last slot then Monday
    fri = booking.next_open_slots(db, UTC_TZ, 30, 14, count=2,
                                  after=datetime(2026, 10, 16, 10, 30), now=NOW)
    assert [s for s, _ in fri] == [datetime(2026, 10, 16, 10, 30), datetime(2026, 10, 19, 9, 0)]


def test_next_open_slots_horizon(db: Session) -> None:
    _window(db, 0, "09:00", "09:30")  # Mondays only; next Monday is 5 days out
    assert booking.next_open_slots(db, UTC_TZ, 30, 4, now=NOW) == []
    assert len(booking.next_open_slots(db, UTC_TZ, 30, 5, now=NOW)) == 1


def test_is_template_slot(db: Session) -> None:
    _window(db, 2, "09:00", "10:00")
    assert booking.is_template_slot(db, datetime(2026, 10, 14, 9, 30), UTC_TZ, 30)
    assert not booking.is_template_slot(db, datetime(2026, 10, 14, 9, 15), UTC_TZ, 30)
    assert not booking.is_template_slot(db, datetime(2026, 10, 14, 10, 0), UTC_TZ, 30)


def test_book_success_local(db: Session) -> None:
    lead = _lead(db)
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0, 33), slot_minutes=45, now=NOW)
    db.commit()
    assert appt.starts_at == datetime(2026, 10, 14, 9, 0)
    assert appt.ends_at == datetime(2026, 10, 14, 9, 45)
    assert appt.title == "Intro call with Ann Lee (Acme)"
    assert appt.provider == "local"
    assert appt.status == "confirmed"
    assert appt.attendee_email == "ann@x.example"
    assert appt.ics_uid.endswith("@clusterx-os")
    assert appt.external_id is None


def test_book_title_fallbacks(db: Session) -> None:
    lead = _lead(db, name="", company="")
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    assert appt.title == "Intro call with ann@x.example"
    other = booking.book(db, lead, datetime(2026, 10, 14, 10, 0), slot_minutes=30, now=NOW,
                         title="Custom")
    assert other.title == "Custom"


def test_book_aware_datetime_converted(db: Session) -> None:
    lead = _lead(db)
    appt = booking.book(db, lead, datetime(2026, 10, 14, 17, 0, tzinfo=MANILA),
                        slot_minutes=30, now=NOW)
    assert appt.starts_at == datetime(2026, 10, 14, 9, 0)


def test_book_rejects_past(db: Session) -> None:
    lead = _lead(db)
    with pytest.raises(booking.BookingError, match="past"):
        booking.book(db, lead, NOW - timedelta(minutes=1), slot_minutes=30, now=NOW)
    # exactly now is allowed
    booking.book(db, lead, NOW, slot_minutes=30, now=NOW)


def test_book_rejects_overlap(db: Session) -> None:
    lead = _lead(db)
    booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    db.commit()
    with pytest.raises(booking.BookingError, match="Conflicts"):
        booking.book(db, lead, datetime(2026, 10, 14, 9, 15), slot_minutes=30, now=NOW)
    # touching intervals do not overlap
    booking.book(db, lead, datetime(2026, 10, 14, 9, 30), slot_minutes=30, now=NOW)
    booking.book(db, lead, datetime(2026, 10, 14, 8, 30), slot_minutes=30, now=NOW)


def test_book_with_adapter(db: Session) -> None:
    lead = _lead(db)
    cal = FakeCalendar()
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, adapter=cal,
                        now=NOW)
    booking.commit_booking(db, appt, cal)
    assert appt.provider == "fake"
    assert appt.external_id == "ext-1"
    event = cal.created[0]
    assert (event.title, event.attendee_email, event.attendee_phone, event.attendee_name) == (
        "Intro call with Ann Lee (Acme)", "ann@x.example", "+15550001111", "Ann Lee")
    assert event.ends_at - event.starts_at == timedelta(minutes=30)


def test_book_adapter_busy(db: Session) -> None:
    lead = _lead(db)
    cal = FakeCalendar(busy=[(datetime(2026, 10, 14, 9, 0), datetime(2026, 10, 14, 10, 0))])
    with pytest.raises(booking.BookingError, match="connected calendar"):
        booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, adapter=cal, now=NOW)


def test_book_upstream_failure_rolls_back_only_booking(db: Session) -> None:
    lead = _lead(db)
    lead.company = "Changed"  # a pending change of the caller must survive
    cal = FakeCalendar(fail_create=True)
    with pytest.raises(booking.BookingError, match="rejected"):
        booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, adapter=cal, now=NOW)
    db.commit()
    assert db.scalars(select(Appointment)).all() == []
    db.refresh(lead)
    assert lead.company == "Changed"


def test_unique_index_blocks_double_booking(db: Session) -> None:
    lead = _lead(db)
    db.add(Appointment(lead_id=lead.id, title="x", starts_at=datetime(2026, 10, 14, 9, 0),
                       ends_at=datetime(2026, 10, 14, 9, 30), status="confirmed",
                       ics_uid="a@x"))
    db.flush()
    # bypass the overlap check to exercise the database guard
    original = booking.busy_intervals
    booking.busy_intervals = lambda *a, **k: []
    try:
        with pytest.raises(booking.BookingError, match="just booked"):
            booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    finally:
        booking.busy_intervals = original
    assert len(db.scalars(select(Appointment)).all()) == 1


def test_commit_booking_failure_cancels_upstream(db: Session) -> None:
    lead = _lead(db)
    cal = FakeCalendar()
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, adapter=cal,
                        now=NOW)

    def broken_commit() -> None:
        raise RuntimeError("db down")

    db.commit = broken_commit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        booking.commit_booking(db, appt, cal)
    assert cal.cancelled == ["ext-1"]


def test_commit_booking_failure_tolerates_cancel_error(db: Session) -> None:
    lead = _lead(db)
    cal = FakeCalendar(fail_cancel=True)
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, adapter=cal,
                        now=NOW)
    db.commit = lambda: (_ for _ in ()).throw(RuntimeError("db down"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="db down"):
        booking.commit_booking(db, appt, cal)


def test_cancel(db: Session) -> None:
    lead = _lead(db)
    cal = FakeCalendar()
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, adapter=cal,
                        now=NOW)
    booking.cancel(db, appt, cal)
    assert appt.status == "cancelled"
    assert appt.cancelled_at is not None
    assert cal.cancelled == ["ext-1"]
    booking.cancel(db, appt, cal)  # idempotent
    assert cal.cancelled == ["ext-1"]


def test_cancel_other_provider_is_local_only(db: Session) -> None:
    lead = _lead(db)
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    appt.external_id = "foreign"
    cal = FakeCalendar()
    booking.cancel(db, appt, cal)
    assert cal.cancelled == []
    assert appt.status == "cancelled"


def test_cancel_upstream_failure(db: Session) -> None:
    lead = _lead(db)
    cal = FakeCalendar(fail_cancel=True)
    appt = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, adapter=cal,
                        now=NOW)
    with pytest.raises(booking.BookingError, match="could not cancel"):
        booking.cancel(db, appt, cal)
    assert appt.status == "confirmed"


def test_ics_escape_and_fold() -> None:
    assert booking.ics_escape("a,b;c\\d\ne\r\nf") == "a\\,b\\;c\\\\d\\ne\\nf"
    short = "X" * 75
    assert booking.fold_line(short) == short
    long_line = "SUMMARY:" + "y" * 200
    folded = booking.fold_line(long_line)
    parts = folded.split("\r\n ")
    assert len(parts[0].encode()) == 75
    assert all(len(p.encode()) <= 74 for p in parts[1:])
    assert "".join(parts) == long_line
    multibyte = "SUMMARY:" + "\u00e9" * 80
    for chunk in booking.fold_line(multibyte).split("\r\n "):
        chunk.encode("utf-8")  # never splits inside a character
    assert "".join(booking.fold_line(multibyte).split("\r\n ")) == multibyte


def test_calendar_ics(db: Session) -> None:
    lead = _lead(db)
    a = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    b = booking.book(db, lead, datetime(2026, 10, 14, 10, 0), slot_minutes=30, now=NOW)
    booking.cancel(db, b)
    body = booking.calendar_ics([a, b], organizer="me@x.example", now=NOW)
    assert body.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert body.endswith("END:VCALENDAR\r\n")
    assert body.count("BEGIN:VEVENT") == 2
    assert "DTSTART:20261014T090000Z" in body
    assert "DTEND:20261014T093000Z" in body
    assert "DTSTAMP:20261014T080000Z" in body
    assert "SUMMARY:Intro call with Ann Lee (Acme)" in body
    assert "ATTENDEE;CN=ann@x.example:mailto:ann@x.example" in body
    assert "ORGANIZER:mailto:me@x.example" in body
    assert "STATUS:CONFIRMED" in body and "STATUS:CANCELLED" in body
    assert f"UID:{a.ics_uid}" in body


def test_calendar_ics_without_attendee_or_organizer(db: Session) -> None:
    lead = _lead(db, email="")
    a = booking.book(db, lead, datetime(2026, 10, 14, 9, 0), slot_minutes=30, now=NOW)
    body = booking.calendar_ics([a], name="Team, A")
    assert "ATTENDEE" not in body
    assert "ORGANIZER" not in body
    assert "X-WR-CALNAME:Team\\, A" in body
    assert booking.calendar_ics([]).count("VEVENT") == 0


def test_dates_are_naive_utc() -> None:
    assert NOW.tzinfo is None
    assert datetime(2026, 1, 1, tzinfo=UTC).tzinfo is UTC
