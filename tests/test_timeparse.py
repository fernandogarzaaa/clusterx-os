from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from clusterx_os.timeparse import parse_clock, parse_day, parse_requested_time

# Wednesday 14 Oct 2026, 10:00 UTC
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
TODAY = NOW.date()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3pm", time(15, 0)),
        ("3 PM", time(15, 0)),
        ("3:30pm", time(15, 30)),
        ("10am", time(10, 0)),
        ("10 a.m.", time(10, 0)),
        ("12pm", time(12, 0)),
        ("12am", time(0, 0)),
        ("1pm", time(13, 0)),
        ("11:59 pm", time(23, 59)),
        ("15:00", time(15, 0)),
        ("09:05", time(9, 5)),
        ("23:59", time(23, 59)),
        ("0:00", time(0, 0)),
        ("at noon", time(12, 0)),
        ("midday works", time(12, 0)),
        ("at 3", time(15, 0)),
        ("at 7", time(19, 0)),
        ("at 8", time(8, 0)),
        ("at 11:30 tomorrow", time(11, 30)),
        ("at 12", time(12, 0)),
        ("at 1 on friday", time(13, 0)),
    ],
)
def test_parse_clock(text: str, expected: time) -> None:
    assert parse_clock(text) == expected


@pytest.mark.parametrize(
    "text",
    ["we have 40 people", "at 3 locations", "budget 5000", "13pm", "0am", "24:00", "", "at 13",
     "at 0", "around 3"],
)
def test_parse_clock_rejects(text: str) -> None:
    assert parse_clock(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tomorrow", date(2026, 10, 15)),
        ("tmrw", date(2026, 10, 15)),
        ("tmr", date(2026, 10, 15)),
        ("day after tomorrow", date(2026, 10, 16)),
        ("today", date(2026, 10, 14)),
        ("tonight", date(2026, 10, 14)),
        ("this afternoon", date(2026, 10, 14)),
        ("this morning", date(2026, 10, 14)),
        ("thursday", date(2026, 10, 15)),
        ("thu", date(2026, 10, 15)),
        ("monday", date(2026, 10, 19)),
        ("wednesday", date(2026, 10, 21)),  # same weekday means next week
        ("tuesday", date(2026, 10, 20)),
        ("sunday", date(2026, 10, 18)),
        ("Oct 20", date(2026, 10, 20)),
        ("october 14th", date(2026, 10, 14)),
        ("oct 13", date(2027, 10, 13)),  # already passed this year
        ("20 oct", date(2026, 10, 20)),
        ("3rd of november", date(2026, 11, 3)),
        ("sept 9", date(2027, 9, 9)),
        ("2026-12-01", date(2026, 12, 1)),
    ],
)
def test_parse_day(text: str, expected: date) -> None:
    assert parse_day(text, TODAY) == expected


@pytest.mark.parametrize("text", ["2026-13-01", "feb 30", "maybe 3", "someday", ""])
def test_parse_day_rejects(text: str) -> None:
    assert parse_day(text, TODAY) is None


def test_requested_time_full() -> None:
    assert parse_requested_time("tomorrow at 3pm", NOW) == datetime(2026, 10, 15, 15, 0,
                                                                    tzinfo=UTC)


def test_requested_time_iso_datetime() -> None:
    assert parse_requested_time("2026-10-20 14:30", NOW) == datetime(2026, 10, 20, 14, 30,
                                                                     tzinfo=UTC)
    assert parse_requested_time("2026-10-20T09:00", NOW) == datetime(2026, 10, 20, 9, 0,
                                                                     tzinfo=UTC)


def test_requested_time_iso_date_with_clock() -> None:
    assert parse_requested_time("2026-10-20 at 4pm", NOW) == datetime(2026, 10, 20, 16, 0,
                                                                      tzinfo=UTC)


def test_requested_time_needs_clock() -> None:
    assert parse_requested_time("tomorrow works", NOW) is None
    assert parse_requested_time("next month", NOW) is None


def test_requested_time_without_day_rolls_forward() -> None:
    assert parse_requested_time("3pm", NOW) == datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
    assert parse_requested_time("9am", NOW) == datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    # exactly "now" is not in the future
    assert parse_requested_time("10:00", NOW) == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)


def test_requested_time_keeps_timezone() -> None:
    manila = ZoneInfo("Asia/Manila")
    now = datetime(2026, 10, 14, 8, 0, tzinfo=manila)
    got = parse_requested_time("friday 2pm", now)
    assert got == datetime(2026, 10, 16, 14, 0, tzinfo=manila)
    assert got is not None and got.tzinfo == manila
