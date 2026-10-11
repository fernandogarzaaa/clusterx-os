"""External calendar adapters.

The built-in calendar (weekday availability templates + the appointments
table + ICS feed) always applies. An external adapter additionally:

* filters candidate slots by real availability (Google free/busy, Cal.com slots)
* creates the event upstream when a booking is made, and cancels it.

All datetimes crossing this module are naive UTC.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from clusterx_os.config import Settings
from clusterx_os.integrations import ConfigurationError, IntegrationError, send

Interval = tuple[datetime, datetime]

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
CALCOM_BOOKINGS_VERSION = "2026-02-25"
CALCOM_SLOTS_VERSION = "2024-09-04"


@dataclass(frozen=True)
class EventRequest:
    title: str
    starts_at: datetime
    ends_at: datetime
    attendee_name: str
    attendee_email: str
    attendee_phone: str
    description: str = ""


class CalendarAdapter(Protocol):
    name: str

    def filter_available(self, candidates: list[Interval]) -> list[Interval]: ...

    def create_event(self, event: EventRequest) -> str: ...

    def cancel_event(self, external_id: str) -> None: ...

    def status(self) -> dict[str, Any]: ...


def to_rfc3339(value: datetime) -> str:
    return value.replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into naive UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


class GoogleCalendar:
    """Google Calendar REST API with an OAuth2 refresh token (offline access)."""

    name = "google"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
        send_updates: str = "none",
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not (client_id and client_secret and refresh_token):
            raise ConfigurationError(
                "Google Calendar needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN"
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.calendar_id = calendar_id
        self.send_updates = send_updates
        self._client = client or httpx.Client(timeout=20.0)
        self._clock = clock
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token_expiry - 60:
                return self._token
            resp = send("google", self._client, "POST", GOOGLE_TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            })
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise IntegrationError("google token response had no access_token")
            self._token = str(token)
            self._token_expiry = self._clock() + float(data.get("expires_in", 3600))
            return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token()}"}

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        resp = send("google", self._client, "POST", f"{GOOGLE_API}/freeBusy",
                    headers=self._headers(),
                    json={"timeMin": to_rfc3339(start), "timeMax": to_rfc3339(end),
                          "items": [{"id": self.calendar_id}]})
        cal = resp.json().get("calendars", {}).get(self.calendar_id, {})
        if cal.get("errors"):
            raise IntegrationError(f"google freeBusy error: {cal['errors']}")
        return [(parse_utc(b["start"]), parse_utc(b["end"])) for b in cal.get("busy", [])]

    def filter_available(self, candidates: list[Interval]) -> list[Interval]:
        if not candidates:
            return []
        busy = self.busy(min(c[0] for c in candidates), max(c[1] for c in candidates))
        return [c for c in candidates if not any(overlaps(c, b) for b in busy)]

    def create_event(self, event: EventRequest) -> str:
        body: dict[str, Any] = {
            "summary": event.title,
            "description": event.description,
            "start": {"dateTime": to_rfc3339(event.starts_at), "timeZone": "UTC"},
            "end": {"dateTime": to_rfc3339(event.ends_at), "timeZone": "UTC"},
        }
        if event.attendee_email:
            body["attendees"] = [
                {"email": event.attendee_email, "displayName": event.attendee_name}
            ]
        resp = send("google", self._client, "POST",
                    f"{GOOGLE_API}/calendars/{self.calendar_id}/events",
                    headers=self._headers(), params={"sendUpdates": self.send_updates}, json=body)
        return str(resp.json()["id"])

    def cancel_event(self, external_id: str) -> None:
        try:
            send("google", self._client, "DELETE",
                 f"{GOOGLE_API}/calendars/{self.calendar_id}/events/{external_id}",
                 headers=self._headers(), params={"sendUpdates": self.send_updates})
        except IntegrationError as exc:
            if exc.status_code not in (404, 410):  # already gone is fine
                raise

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": True, "calendar_id": self.calendar_id}


class CalComCalendar:
    """Cal.com API v2: availability from /v2/slots, bookings via /v2/bookings."""

    name = "calcom"

    def __init__(
        self,
        api_key: str,
        event_type_id: int,
        base_url: str = "https://api.cal.com",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or event_type_id <= 0:
            raise ConfigurationError("Cal.com needs CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID")
        self.api_key = api_key
        self.event_type_id = event_type_id
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=20.0)

    def _headers(self, version: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "cal-api-version": version}

    def available_starts(self, start: datetime, end: datetime) -> set[datetime]:
        resp = send("calcom", self._client, "GET", f"{self.base_url}/v2/slots",
                    headers=self._headers(CALCOM_SLOTS_VERSION),
                    params={"eventTypeId": self.event_type_id, "start": to_rfc3339(start),
                            "end": to_rfc3339(end), "timeZone": "UTC"})
        data = resp.json().get("data", {})
        starts: set[datetime] = set()
        if not isinstance(data, dict):
            return starts
        for slots in data.values():
            for slot in slots or []:
                raw = slot.get("start") if isinstance(slot, dict) else slot
                if not isinstance(raw, str):
                    continue
                try:
                    starts.add(parse_utc(raw).replace(second=0, microsecond=0))
                except ValueError:
                    continue
        return starts

    def filter_available(self, candidates: list[Interval]) -> list[Interval]:
        if not candidates:
            return []
        starts = self.available_starts(
            min(c[0] for c in candidates), max(c[1] for c in candidates)
        )
        return [c for c in candidates if c[0].replace(second=0, microsecond=0) in starts]

    def create_event(self, event: EventRequest) -> str:
        if not event.attendee_email:
            raise IntegrationError("Cal.com bookings need an attendee email", retryable=False)
        attendee: dict[str, Any] = {
            "name": event.attendee_name or event.attendee_email,
            "email": event.attendee_email,
            "timeZone": "UTC",
        }
        if event.attendee_phone:
            attendee["phoneNumber"] = event.attendee_phone
        body = {
            "start": to_rfc3339(event.starts_at),
            "eventTypeId": self.event_type_id,
            "attendee": attendee,
            "metadata": {"source": "clusterx-os"},
        }
        resp = send("calcom", self._client, "POST", f"{self.base_url}/v2/bookings",
                    headers=self._headers(CALCOM_BOOKINGS_VERSION), json=body)
        data = resp.json().get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        uid = data.get("uid")
        if not uid:
            raise IntegrationError("Cal.com booking response had no uid")
        return str(uid)

    def cancel_event(self, external_id: str) -> None:
        send("calcom", self._client, "POST", f"{self.base_url}/v2/bookings/{external_id}/cancel",
             headers=self._headers(CALCOM_BOOKINGS_VERSION),
             json={"cancellationReason": "Cancelled from ClusterX OS"})

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": True, "event_type_id": self.event_type_id}


def get_calendar(settings: Settings) -> CalendarAdapter | None:
    """External calendar adapter, or None for the built-in calendar only."""
    if settings.CALENDAR_PROVIDER == "google":
        return GoogleCalendar(settings.GOOGLE_CLIENT_ID, settings.GOOGLE_CLIENT_SECRET,
                              settings.GOOGLE_REFRESH_TOKEN, settings.GOOGLE_CALENDAR_ID,
                              settings.GOOGLE_SEND_UPDATES)
    if settings.CALENDAR_PROVIDER == "calcom":
        return CalComCalendar(settings.CALCOM_API_KEY, settings.CALCOM_EVENT_TYPE_ID,
                              settings.CALCOM_BASE_URL)
    return None


def calendar_status(settings: Settings) -> dict[str, Any]:
    if settings.CALENDAR_PROVIDER == "google":
        ok = bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET
                  and settings.GOOGLE_REFRESH_TOKEN)
        return {"name": "google", "configured": ok,
                "detail": f"Free/busy + events on calendar '{settings.GOOGLE_CALENDAR_ID}'."}
    if settings.CALENDAR_PROVIDER == "calcom":
        ok = bool(settings.CALCOM_API_KEY and settings.CALCOM_EVENT_TYPE_ID > 0)
        return {"name": "calcom", "configured": ok,
                "detail": f"Slots + bookings for event type {settings.CALCOM_EVENT_TYPE_ID}."}
    return {"name": "local", "configured": True,
            "detail": "Built-in calendar: availability templates, bookings, ICS feed."}
