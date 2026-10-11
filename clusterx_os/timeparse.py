"""Parse a meeting time a lead typed ("tomorrow at 3pm", "Tue 10:30", "Oct 15 2pm")."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}  # fmt: skip
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_MONTH_RE = (
    r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug"
    r"|september|sept|sep|october|oct|november|nov|december|dec)\b\.?"
)

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2}))?")
_AMPM_RE = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])")
_24H_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_AT_RE = re.compile(
    r"\bat\s+(\d{1,2})(?::([0-5]\d))?(?=\s*(?:$|[,.!?;)]|o'?clock\b|on\b|tomorrow\b|today\b"
    r"|tonight\b|next\b|this\b|(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b))"
)
_MONTH_DAY_RE = re.compile(_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE)
_WEEKDAY_RE = re.compile(
    r"\b(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|"
    r"saturday|sat|sunday|sun)\b"
)


def parse_clock(text: str) -> time | None:
    """Extract a clock time. A bare 'at 3' means 3pm (business hours)."""
    t = text.lower()
    if re.search(r"\bnoon\b|\bmidday\b", t):
        return time(12, 0)
    m = _AMPM_RE.search(t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12:
            return None
        is_pm = m.group(3).startswith("p")
        if hour == 12:
            hour = 12 if is_pm else 0
        elif is_pm:
            hour += 12
        return time(hour, minute)
    m = _24H_RE.search(t)
    if m:
        return time(int(m.group(1)), int(m.group(2)))
    m = _AT_RE.search(t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12:
            return None
        if hour < 8:
            hour += 12
        return time(hour, minute)
    return None


def _next_month_day(month: int, day: int, today: date) -> date | None:
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None  # pragma: no cover - a valid date always recurs next year


def parse_day(text: str, today: date) -> date | None:
    t = text.lower()
    m = _ISO_RE.search(t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b|\btmrw\b|\btmr\b", t):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b|\btonight\b|\bthis afternoon\b|\bthis morning\b", t):
        return today
    m = _MONTH_DAY_RE.search(t)
    if m:
        return _next_month_day(_MONTHS[m.group(1)[:3]], int(m.group(2)), today)
    m = _DAY_MONTH_RE.search(t)
    if m:
        return _next_month_day(_MONTHS[m.group(2)[:3]], int(m.group(1)), today)
    m = _WEEKDAY_RE.search(t)
    if m:
        ahead = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    return None


def parse_requested_time(text: str, now: datetime) -> datetime | None:
    """Return the requested meeting start in ``now``'s timezone, or None.

    A clock time is required. Without a day, the next occurrence of that time is used.
    """
    t = text.lower()
    iso = _ISO_RE.search(t)
    if iso and iso.group(4) and int(iso.group(4)) < 24:
        clock: time | None = time(int(iso.group(4)), int(iso.group(5)))
    else:
        clock = parse_clock(t)
    if clock is None:
        return None
    day = parse_day(t, now.date())
    if day is None:
        candidate = datetime.combine(now.date(), clock, tzinfo=now.tzinfo)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return datetime.combine(day, clock, tzinfo=now.tzinfo)
