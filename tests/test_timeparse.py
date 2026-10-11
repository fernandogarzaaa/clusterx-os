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


# Monday 12 Oct 2026; every weekday spelling maps to the right day of the week ahead.
_MONDAY = date(2026, 10, 12)


@pytest.mark.parametrize(
    ("word", "offset"),
    [
        ("monday", 7), ("mon", 7), ("tuesday", 1), ("tue", 1), ("tues", 1),
        ("wednesday", 2), ("wed", 2), ("thursday", 3), ("thu", 3), ("thur", 3),
        ("thurs", 3), ("friday", 4), ("fri", 4), ("saturday", 5), ("sat", 5),
        ("sunday", 6), ("sun", 6),
    ],
)
def test_every_weekday_spelling(word: str, offset: int) -> None:
    from datetime import timedelta

    assert parse_day(f"see you {word}", _MONDAY) == _MONDAY + timedelta(days=offset)


def test_invalid_iso_date_wins_over_other_day_words() -> None:
    # An explicit but impossible date is rejected outright, not reinterpreted.
    assert parse_day("2026-02-30 tomorrow", TODAY) is None


def test_iso_clock_takes_precedence_over_other_clock_text() -> None:
    assert parse_requested_time("2026-10-20 09:05", NOW) == datetime(
        2026, 10, 20, 9, 5, tzinfo=UTC
    )
    # Hour 24 is not an ISO clock time; the parser must not crash on it.
    assert parse_requested_time("2026-10-20 24:00", NOW) is None
    assert parse_requested_time("2026-10-20 24:00 at 3pm", NOW) == datetime(
        2026, 10, 20, 15, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("word", "month"),
    [
        ("january", 1), ("jan", 1), ("february", 2), ("feb", 2), ("march", 3), ("mar", 3),
        ("april", 4), ("apr", 4), ("may", 5), ("june", 6), ("jun", 6), ("july", 7),
        ("jul", 7), ("august", 8), ("aug", 8), ("september", 9), ("sept", 9), ("sep", 9),
        ("october", 10), ("oct", 10), ("november", 11), ("nov", 11), ("december", 12),
        ("dec", 12),
    ],
)
def test_every_month_spelling(word: str, month: int) -> None:
    # 20th of each month, counted from 14 Oct 2026: later months stay in 2026.
    expected_year = 2026 if month >= 10 else 2027
    assert parse_day(f"{word} 20", TODAY) == date(expected_year, month, 20)
    assert parse_day(f"20 {word}", TODAY) == date(expected_year, month, 20)
