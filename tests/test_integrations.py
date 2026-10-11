"""Adapter tests against recorded provider payloads (httpx.MockTransport, no network)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from clusterx_os.config import Settings
from clusterx_os.integrations import (
    ConfigurationError,
    IntegrationError,
    calendars,
    channels,
    crm,
    llm,
    send,
)
from clusterx_os.integrations.calendars import CalComCalendar, EventRequest, GoogleCalendar
from clusterx_os.integrations.channels import TwilioAdapter, WhatsAppAdapter
from clusterx_os.integrations.crm import HubSpotCRM, LeadSnapshot
from clusterx_os.integrations.llm import MockLLM, OpenAICompatibleLLM, extract_json_object

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def body(self, i: int) -> Any:
        return json.loads(self.requests[i].content)


def ok(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


EVENT = EventRequest(title="Intro", starts_at=datetime(2026, 10, 14, 9, 0),
                     ends_at=datetime(2026, 10, 14, 9, 30), attendee_name="Ann",
                     attendee_email="ann@x.example", attendee_phone="+15550001111",
                     description="d")


# ------------------------------------------------------------- shared ---

def test_send_maps_errors() -> None:
    rec = Recorder(httpx.Response(429, text="slow"), httpx.Response(503), httpx.Response(400,
                   text="bad"), httpx.ConnectError("down"), httpx.ReadTimeout("slow"))
    c = rec.client()
    with pytest.raises(IntegrationError) as e429:
        send("p", c, "GET", "https://x/")
    assert e429.value.retryable and e429.value.status_code == 429
    assert "p API error 429: slow" in str(e429.value)
    with pytest.raises(IntegrationError) as e503:
        send("p", c, "GET", "https://x/")
    assert e503.value.retryable and e503.value.status_code == 503
    with pytest.raises(IntegrationError) as e400:
        send("p", c, "GET", "https://x/")
    assert not e400.value.retryable and e400.value.status_code == 400
    with pytest.raises(IntegrationError) as econn:
        send("p", c, "GET", "https://x/")
    assert econn.value.retryable and econn.value.status_code is None
    with pytest.raises(IntegrationError, match="timed out") as etime:
        send("p", c, "GET", "https://x/")
    assert etime.value.retryable


def test_send_success_and_499_boundary() -> None:
    rec = Recorder(ok({"a": 1}, 201), httpx.Response(399), httpx.Response(499), httpx.Response(500))
    c = rec.client()
    assert send("p", c, "POST", "https://x/").json() == {"a": 1}
    assert send("p", c, "POST", "https://x/").status_code == 399
    with pytest.raises(IntegrationError) as e:
        send("p", c, "POST", "https://x/")
    assert e.value.retryable is False
    with pytest.raises(IntegrationError) as e2:
        send("p", c, "POST", "https://x/")
    assert e2.value.retryable is True


# ---------------------------------------------------------------- LLM ---

def test_extract_json_object() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('Sure! {"a": 3} hope that helps') == {"a": 3}
    assert extract_json_object("[1, 2]") is None
    assert extract_json_object("no json") is None
    assert extract_json_object("}{") is None


def test_openai_llm_request_and_parse() -> None:
    rec = Recorder(ok({"choices": [{"message": {"content": '{"reply": "hi"}'}}]}))
    client = OpenAICompatibleLLM("https://api.groq.com/openai/v1/", "k", "m", client=rec.client())
    assert client.complete_json("sys", "usr") == {"reply": "hi"}
    req = rec.requests[0]
    assert str(req.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer k"
    body = rec.body(0)
    assert body["model"] == "m"
    assert body["temperature"] == 0.2
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "usr"}]
    assert client.status()["model"] == "m"


def test_openai_llm_without_key_or_json_mode() -> None:
    rec = Recorder(ok({"choices": [{"message": {"content": "{}"}}]}))
    client = OpenAICompatibleLLM("http://localhost:11434/v1", "", "llama3", json_mode=False,
                                 client=rec.client())
    assert client.complete_json("s", "u") == {}
    assert "Authorization" not in rec.requests[0].headers
    assert "response_format" not in rec.body(0)


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), ok({"choices": []}), ok({"nope": 1}),
     ok({"choices": [{"message": {"content": None}}]}), ok({"choices": [{"message":
                                                                        {"content": "plain"}}]}),
     httpx.ConnectError("x")],
)
def test_openai_llm_failures_return_none(response: httpx.Response | Exception) -> None:
    client = OpenAICompatibleLLM("https://x/v1", "k", "m", client=Recorder(response).client())
    assert client.complete_json("s", "u") is None


def test_openai_chat_raises_integration_error_on_bad_payload() -> None:
    client = OpenAICompatibleLLM("https://x/v1", "k", "m",
                                 client=Recorder(ok({"choices": []})).client())
    with pytest.raises(IntegrationError, match="unexpected payload"):
        client.chat([])
    client2 = OpenAICompatibleLLM("https://x/v1", "k", "m", client=Recorder(
        ok({"choices": [{"message": {"content": 5}}]})).client())
    with pytest.raises(IntegrationError, match="non-text"):
        client2.chat([])


def test_mock_llm() -> None:
    m = MockLLM()
    out = m.complete_json("s", json.dumps({"draft_reply": " hi ",
                                           "rule_intent": {"label": "Warm", "confidence": 0.7}}))
    assert out == {"reply": "hi", "intent": "Warm", "confidence": 0.7,
                   "reason": "mock llm agreed with rule engine", "slots": {}}
    assert m.complete_json("s", json.dumps({"draft_reply": ""})) is None
    assert m.complete_json("s", "not json") is None
    assert m.complete_json("s", json.dumps({"draft_reply": "x"}))["intent"] == "Cold"  # type: ignore[index]
    assert m.status()["configured"] is True


def test_get_llm(settings: Settings) -> None:
    assert llm.get_llm(settings) is None
    assert isinstance(llm.get_llm(settings.model_copy(update={"LLM_PROVIDER": "mock"})), MockLLM)
    assert llm.get_llm(settings.model_copy(update={"LLM_PROVIDER": "openai"})) is None
    real = llm.get_llm(settings.model_copy(update={
        "LLM_PROVIDER": "openai", "LLM_BASE_URL": "https://openrouter.ai/api/v1",
        "LLM_MODEL": "x", "LLM_JSON_MODE": False}))
    assert isinstance(real, OpenAICompatibleLLM)
    assert real.base_url == "https://openrouter.ai/api/v1"
    assert real.json_mode is False


# ------------------------------------------------------------ HubSpot ---

LEAD = LeadSnapshot(id=1, name="Ann Marie Lee", email="ann@x.example", phone="+1555",
                    company="Acme", stage="Qualified", intent="Hot", external_id=None)


def test_hubspot_properties() -> None:
    props = crm.hubspot_properties(LEAD)
    assert props == {"firstname": "Ann", "lastname": "Marie Lee", "email": "ann@x.example",
                     "phone": "+1555", "company": "Acme", "hs_lead_status": "IN_PROGRESS"}
    won = crm.hubspot_properties(LeadSnapshot(**{**LEAD.__dict__, "stage": "Won", "name": "",
                                                 "company": ""}))
    assert won["lifecyclestage"] == "customer"
    assert "firstname" not in won and "company" not in won
    unq = crm.hubspot_properties(LeadSnapshot(**{**LEAD.__dict__, "intent": "Unqualified"}))
    assert unq["hs_lead_status"] == "UNQUALIFIED"
    odd = crm.hubspot_properties(LeadSnapshot(**{**LEAD.__dict__, "stage": "Weird"}))
    assert odd["hs_lead_status"] == "NEW"
    assert crm.split_name("  ") == ("", "")
    assert crm.split_name("Cher") == ("Cher", "")


@pytest.mark.parametrize(("stage", "status"), list(crm.HUBSPOT_LEAD_STATUS.items()))
def test_hubspot_stage_map(stage: str, status: str) -> None:
    lead = LeadSnapshot(**{**LEAD.__dict__, "stage": stage, "intent": None})
    assert crm.hubspot_properties(lead)["hs_lead_status"] == status


def test_hubspot_create_when_not_found() -> None:
    rec = Recorder(ok({"results": []}), ok({"id": "901"}, 201))
    hub = HubSpotCRM("tok", "https://api.hubapi.com/", client=rec.client())
    assert hub.sync_lead(LEAD) == "901"
    search, create = rec.requests
    assert str(search.url) == "https://api.hubapi.com/crm/v3/objects/contacts/search"
    assert search.headers["Authorization"] == "Bearer tok"
    assert rec.body(0)["filterGroups"][0]["filters"][0] == {
        "propertyName": "email", "operator": "EQ", "value": "ann@x.example"}
    assert create.method == "POST" and create.url.path == "/crm/v3/objects/contacts"
    assert rec.body(1)["properties"]["email"] == "ann@x.example"


def test_hubspot_updates_found_contact() -> None:
    rec = Recorder(ok({"results": [{"id": 77}]}), ok({"id": "77"}))
    hub = HubSpotCRM("tok", client=rec.client())
    assert hub.sync_lead(LEAD) == "77"
    assert rec.requests[1].method == "PATCH"
    assert rec.requests[1].url.path == "/crm/v3/objects/contacts/77"


def test_hubspot_uses_known_id_without_search() -> None:
    rec = Recorder(ok({"id": "5"}))
    hub = HubSpotCRM("tok", client=rec.client())
    assert hub.sync_lead(LeadSnapshot(**{**LEAD.__dict__, "external_id": "5"})) == "5"
    assert len(rec.requests) == 1 and rec.requests[0].method == "PATCH"


def test_hubspot_recreates_deleted_contact() -> None:
    rec = Recorder(httpx.Response(404, text="gone"), ok({"id": "6"}, 201))
    hub = HubSpotCRM("tok", client=rec.client())
    assert hub.sync_lead(LeadSnapshot(**{**LEAD.__dict__, "external_id": "5"})) == "6"


def test_hubspot_patch_error_propagates() -> None:
    hub = HubSpotCRM("tok", client=Recorder(httpx.Response(500)).client())
    with pytest.raises(IntegrationError) as exc:
        hub.sync_lead(LeadSnapshot(**{**LEAD.__dict__, "external_id": "5"}))
    assert exc.value.retryable


def test_hubspot_conflict_uses_existing_id() -> None:
    rec = Recorder(httpx.Response(409, text='{"message":"Contact already exists. Existing ID: 123"}'),
                   ok({"id": "123"}))
    hub = HubSpotCRM("tok", client=rec.client())
    assert hub.sync_lead(LeadSnapshot(**{**LEAD.__dict__, "email": ""})) == "123"
    assert rec.requests[1].url.path == "/crm/v3/objects/contacts/123"


def test_hubspot_create_error_without_existing_id() -> None:
    hub = HubSpotCRM("tok", client=Recorder(httpx.Response(409, text="conflict")).client())
    with pytest.raises(IntegrationError):
        hub.sync_lead(LeadSnapshot(**{**LEAD.__dict__, "email": ""}))
    hub2 = HubSpotCRM("tok", client=Recorder(httpx.Response(400, text="Existing ID: 9")).client())
    with pytest.raises(IntegrationError):
        hub2.sync_lead(LeadSnapshot(**{**LEAD.__dict__, "email": ""}))


def test_hubspot_requires_contact_point() -> None:
    hub = HubSpotCRM("tok", client=Recorder().client())
    with pytest.raises(IntegrationError) as exc:
        hub.sync_lead(LeadSnapshot(**{**LEAD.__dict__, "email": "", "phone": ""}))
    assert exc.value.retryable is False


def test_hubspot_note() -> None:
    rec = Recorder(ok({"id": "n1"}, 201))
    hub = HubSpotCRM("tok", client=rec.client())
    assert hub.add_note("77", "x" * 70000) == "n1"
    body = rec.body(0)
    assert len(body["properties"]["hs_note_body"]) == 65000
    assert body["properties"]["hs_timestamp"].endswith("Z")
    assert body["associations"][0]["to"] == {"id": "77"}
    assert body["associations"][0]["types"][0] == {"associationCategory": "HUBSPOT_DEFINED",
                                                   "associationTypeId": 202}
    assert hub.status()["name"] == "hubspot"


def test_hubspot_requires_token(settings: Settings) -> None:
    with pytest.raises(ConfigurationError):
        HubSpotCRM("")
    assert crm.get_crm(settings) is None
    hub = crm.get_crm(settings.model_copy(update={"CRM_PROVIDER": "hubspot",
                                                  "HUBSPOT_ACCESS_TOKEN": "t"}))
    assert isinstance(hub, HubSpotCRM)
    assert crm.crm_status(settings)["name"] == "local"
    st = crm.crm_status(settings.model_copy(update={"CRM_PROVIDER": "hubspot"}))
    assert st == {"name": "hubspot", "configured": False,
                  "detail": "Leads mirror to HubSpot via the outbox worker."}


# ------------------------------------------------------------- Google ---

def _google(rec: Recorder, clock: Callable[[], float] = lambda: 1000.0) -> GoogleCalendar:
    return GoogleCalendar("cid", "csecret", "rtok", "team@x.example", "all",
                          client=rec.client(), clock=clock)


def test_google_token_refresh_and_cache() -> None:
    t = [1000.0]
    rec = Recorder(ok({"access_token": "a1", "expires_in": 3600}),
                   ok({"calendars": {"team@x.example": {"busy": []}}}),
                   ok({"calendars": {"team@x.example": {"busy": []}}}),
                   ok({"access_token": "a2", "expires_in": 3600}),
                   ok({"calendars": {"team@x.example": {"busy": []}}}))
    g = _google(rec, lambda: t[0])
    g.busy(datetime(2026, 10, 14), datetime(2026, 10, 15))
    g.busy(datetime(2026, 10, 14), datetime(2026, 10, 15))
    assert [r.url.path for r in rec.requests].count("/token") == 1
    form = parse_qs(rec.requests[0].content.decode())
    assert form == {"client_id": ["cid"], "client_secret": ["csecret"],
                    "refresh_token": ["rtok"], "grant_type": ["refresh_token"]}
    t[0] = 1000.0 + 3600 - 59  # inside the 60s safety margin -> refresh
    g.busy(datetime(2026, 10, 14), datetime(2026, 10, 15))
    assert rec.requests[-1].headers["Authorization"] == "Bearer a2"


def test_google_token_still_valid_just_before_margin() -> None:
    t = [1000.0]
    rec = Recorder(ok({"access_token": "a1", "expires_in": 3600}),
                   ok({"calendars": {}}), ok({"calendars": {}}))
    g = _google(rec, lambda: t[0])
    g.busy(datetime(2026, 10, 14), datetime(2026, 10, 15))
    t[0] = 1000.0 + 3600 - 61
    g.busy(datetime(2026, 10, 14), datetime(2026, 10, 15))
    assert len(rec.requests) == 3


def test_google_token_errors() -> None:
    g = _google(Recorder(ok({"token_type": "Bearer"})))
    with pytest.raises(IntegrationError, match="no access_token"):
        g.access_token()
    with pytest.raises(ConfigurationError):
        GoogleCalendar("", "s", "r")


def test_google_freebusy_filter() -> None:
    rec = Recorder(ok({"access_token": "a"}), ok({"calendars": {"team@x.example": {"busy": [
        {"start": "2026-10-14T09:15:00Z", "end": "2026-10-14T09:45:00Z"},
        {"start": "2026-10-14T19:00:00+08:00", "end": "2026-10-14T20:00:00+08:00"}]}}}))
    g = _google(rec)
    cands = [(datetime(2026, 10, 14, h, m), datetime(2026, 10, 14, h, m) + (
        datetime(2026, 1, 1, 0, 30) - datetime(2026, 1, 1))) for h, m in
        [(9, 0), (9, 30), (10, 0), (11, 0), (11, 30), (12, 0)]]
    free = g.filter_available(cands)
    assert [c[0].strftime("%H:%M") for c in free] == ["10:00", "12:00"]
    body = rec.body(1)
    assert body == {"timeMin": "2026-10-14T09:00:00Z", "timeMax": "2026-10-14T12:30:00Z",
                    "items": [{"id": "team@x.example"}]}
    assert g.filter_available([]) == []


def test_google_freebusy_errors() -> None:
    g = _google(Recorder(ok({"access_token": "a"}), ok({"calendars": {"team@x.example": {
        "errors": [{"reason": "notFound"}]}}})))
    with pytest.raises(IntegrationError, match="freeBusy"):
        g.busy(datetime(2026, 10, 14), datetime(2026, 10, 15))


def test_google_create_and_cancel() -> None:
    rec = Recorder(ok({"access_token": "a"}), ok({"id": "evt1"}), httpx.Response(204),
                   httpx.Response(410), httpx.Response(500))
    g = _google(rec)
    assert g.create_event(EVENT) == "evt1"
    req = rec.requests[1]
    assert req.url.path == "/calendar/v3/calendars/team@x.example/events"
    assert req.url.params["sendUpdates"] == "all"
    body = rec.body(1)
    assert body["start"] == {"dateTime": "2026-10-14T09:00:00Z", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2026-10-14T09:30:00Z", "timeZone": "UTC"}
    assert body["attendees"] == [{"email": "ann@x.example", "displayName": "Ann"}]
    assert body["summary"] == "Intro" and body["description"] == "d"
    g.cancel_event("evt1")
    assert rec.requests[2].method == "DELETE"
    g.cancel_event("evt1")  # 410 gone is fine
    with pytest.raises(IntegrationError):
        g.cancel_event("evt1")
    assert g.status()["calendar_id"] == "team@x.example"


def test_google_create_without_attendee() -> None:
    rec = Recorder(ok({"access_token": "a"}), ok({"id": "e"}))
    g = _google(rec)
    g.create_event(EventRequest(**{**EVENT.__dict__, "attendee_email": ""}))
    assert "attendees" not in rec.body(1)


# ------------------------------------------------------------ Cal.com ---

def test_calcom_slots_and_filter() -> None:
    rec = Recorder(ok({"status": "success", "data": {
        "2026-10-14": [{"start": "2026-10-14T09:00:00.000Z"}, "2026-10-14T10:00:00Z",
                       {"start": 5}, {"start": "garbage"}],
        "2026-10-15": None}}))
    cal = CalComCalendar("cal_key", 42, "https://api.cal.com/", client=rec.client())
    cands = [(datetime(2026, 10, 14, 9, 0), datetime(2026, 10, 14, 9, 30)),
             (datetime(2026, 10, 14, 9, 30), datetime(2026, 10, 14, 10, 0)),
             (datetime(2026, 10, 14, 10, 0), datetime(2026, 10, 14, 10, 30))]
    assert [c[0].hour for c in cal.filter_available(cands)] == [9, 10]
    req = rec.requests[0]
    assert req.url.path == "/v2/slots"
    assert req.headers["cal-api-version"] == "2024-09-04"
    assert req.headers["Authorization"] == "Bearer cal_key"
    assert req.url.params["eventTypeId"] == "42"
    assert req.url.params["start"] == "2026-10-14T09:00:00Z"
    assert req.url.params["end"] == "2026-10-14T10:30:00Z"
    assert req.url.params["timeZone"] == "UTC"
    assert cal.filter_available([]) == []


def test_calcom_unexpected_slot_payload() -> None:
    cal = CalComCalendar("k", 1, client=Recorder(ok({"data": []})).client())
    assert cal.available_starts(datetime(2026, 1, 1), datetime(2026, 1, 2)) == set()


def test_calcom_booking() -> None:
    rec = Recorder(ok({"status": "success", "data": {"uid": "bk_1"}}, 201),
                   ok({"status": "success", "data": [{"uid": "bk_2"}]}, 201),
                   ok({"status": "success"}))
    cal = CalComCalendar("k", 42, client=rec.client())
    assert cal.create_event(EVENT) == "bk_1"
    body = rec.body(0)
    assert rec.requests[0].headers["cal-api-version"] == "2026-02-25"
    assert body == {"start": "2026-10-14T09:00:00Z", "eventTypeId": 42,
                    "attendee": {"name": "Ann", "email": "ann@x.example", "timeZone": "UTC",
                                 "phoneNumber": "+15550001111"},
                    "metadata": {"source": "clusterx-os"}}
    assert cal.create_event(EventRequest(**{**EVENT.__dict__, "attendee_phone": "",
                                            "attendee_name": ""})) == "bk_2"
    assert "phoneNumber" not in rec.body(1)["attendee"]
    assert rec.body(1)["attendee"]["name"] == "ann@x.example"
    cal.cancel_event("bk_1")
    assert rec.requests[2].url.path == "/v2/bookings/bk_1/cancel"
    assert rec.body(2) == {"cancellationReason": "Cancelled from ClusterX OS"}


def test_calcom_booking_errors() -> None:
    cal = CalComCalendar("k", 42, client=Recorder(ok({"data": {}}), ok({"data": []})).client())
    with pytest.raises(IntegrationError, match="no uid"):
        cal.create_event(EVENT)
    with pytest.raises(IntegrationError, match="no uid"):
        cal.create_event(EVENT)
    with pytest.raises(IntegrationError, match="attendee email") as exc:
        cal.create_event(EventRequest(**{**EVENT.__dict__, "attendee_email": ""}))
    assert exc.value.retryable is False
    with pytest.raises(ConfigurationError):
        CalComCalendar("k", 0)
    with pytest.raises(ConfigurationError):
        CalComCalendar("", 5)
    assert CalComCalendar("k", 5).status()["event_type_id"] == 5


def test_calendar_factory_and_status(settings: Settings) -> None:
    assert calendars.get_calendar(settings) is None
    g = calendars.get_calendar(settings.model_copy(update={
        "CALENDAR_PROVIDER": "google", "GOOGLE_CLIENT_ID": "a", "GOOGLE_CLIENT_SECRET": "b",
        "GOOGLE_REFRESH_TOKEN": "c"}))
    assert isinstance(g, GoogleCalendar)
    c = calendars.get_calendar(settings.model_copy(update={
        "CALENDAR_PROVIDER": "calcom", "CALCOM_API_KEY": "k", "CALCOM_EVENT_TYPE_ID": 3}))
    assert isinstance(c, CalComCalendar)
    assert calendars.calendar_status(settings)["name"] == "local"
    assert calendars.calendar_status(settings.model_copy(
        update={"CALENDAR_PROVIDER": "google"}))["configured"] is False
    assert calendars.calendar_status(settings.model_copy(update={
        "CALENDAR_PROVIDER": "calcom", "CALCOM_API_KEY": "k", "CALCOM_EVENT_TYPE_ID": 3,
    }))["configured"] is True


def test_time_helpers() -> None:
    assert calendars.to_rfc3339(datetime(2026, 1, 2, 3, 4, 5, 999)) == "2026-01-02T03:04:05Z"
    assert calendars.parse_utc("2026-01-02T10:00:00+02:00") == datetime(2026, 1, 2, 8, 0)
    assert calendars.parse_utc("2026-01-02T10:00:00") == datetime(2026, 1, 2, 10, 0)
    a = (datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10))
    assert calendars.overlaps(a, (datetime(2026, 1, 1, 9, 59), datetime(2026, 1, 1, 11)))
    assert not calendars.overlaps(a, (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11)))
    assert not calendars.overlaps(a, (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9)))


# ----------------------------------------------------------- channels ---

def test_twilio_send() -> None:
    rec = Recorder(ok({"sid": "SM1"}, 201), ok({}, 201))
    tw = TwilioAdapter("AC1", "tok", "+15550000000", client=rec.client())
    assert tw.send("+15551112222", "hello") == "SM1"
    req = rec.requests[0]
    assert req.url.path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"AC1:tok").decode()
    assert parse_qs(req.content.decode()) == {"From": ["+15550000000"], "To": ["+15551112222"],
                                              "Body": ["hello"]}
    assert tw.send("+1", "x") is None


def test_twilio_whatsapp_sender_prefixes_recipient() -> None:
    rec = Recorder(ok({"sid": "SM2"}), ok({"sid": "SM3"}))
    tw = TwilioAdapter("AC1", "tok", "whatsapp:+14155238886", client=rec.client())
    tw.send("+15551112222", "hi")
    tw.send("whatsapp:+15551112222", "hi")
    assert parse_qs(rec.requests[0].content.decode())["To"] == ["whatsapp:+15551112222"]
    assert parse_qs(rec.requests[1].content.decode())["To"] == ["whatsapp:+15551112222"]


def test_twilio_errors() -> None:
    with pytest.raises(ConfigurationError, match="TWILIO_AUTH_TOKEN, TWILIO_FROM"):
        TwilioAdapter("AC1", "", "").send("+1", "x")
    with pytest.raises(IntegrationError, match="no phone"):
        TwilioAdapter("AC1", "t", "+1", client=Recorder().client()).send("", "x")
    st = TwilioAdapter("", "", "").status()
    assert st["configured"] is False and "TWILIO_ACCOUNT_SID" in st["detail"]
    assert TwilioAdapter("a", "b", "c").status() == {
        "name": "twilio", "configured": True, "description": TwilioAdapter.description,
        "detail": "Credentials present."}


def test_whatsapp_send() -> None:
    rec = Recorder(ok({"messages": [{"id": "wamid.1"}]}), ok({}))
    wa = WhatsAppAdapter("tok", "123", "v21.0", client=rec.client())
    assert wa.send("+15551112222", "hey") == "wamid.1"
    req = rec.requests[0]
    assert str(req.url) == "https://graph.facebook.com/v21.0/123/messages"
    assert req.headers["Authorization"] == "Bearer tok"
    assert rec.body(0) == {"messaging_product": "whatsapp", "to": "15551112222", "type": "text",
                           "text": {"body": "hey"}}
    assert wa.send("+1", "x") is None


def test_whatsapp_errors() -> None:
    with pytest.raises(ConfigurationError, match="WHATSAPP_PHONE_NUMBER_ID"):
        WhatsAppAdapter("t", "").send("+1", "x")
    with pytest.raises(IntegrationError):
        WhatsAppAdapter("t", "1", client=Recorder().client()).send("", "x")
    assert WhatsAppAdapter("", "").status()["configured"] is False
    assert WhatsAppAdapter("t", "1").status()["configured"] is True


def test_simulator_and_registry(settings: Settings) -> None:
    sim = channels.SimulatorAdapter()
    assert sim.send("+1", "x") is None
    assert sim.status()["configured"] is True
    adapters = channels.build_adapters(settings)
    assert sorted(adapters) == ["simulator", "twilio", "whatsapp"]
    statuses = channels.adapter_statuses(settings)
    assert [s["name"] for s in statuses] == ["simulator", "twilio", "whatsapp"]
    assert [s["configured"] for s in statuses] == [True, False, False]


def test_twilio_signature_matches_reference() -> None:
    # Algorithm from Twilio's request-validation docs, computed independently here.
    token = "12345"
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    expected = base64.b64encode(hmac.new(
        token.encode(), (url + "".join(f"{k}{params[k]}" for k in sorted(params))).encode(),
        hashlib.sha1).digest()).decode()
    assert channels.twilio_signature(token, url, params) == expected
    assert channels.verify_twilio_signature(token, url, params, expected)
    assert not channels.verify_twilio_signature(token, url, params, "bad")
    assert not channels.verify_twilio_signature("", url, params, expected)
    assert not channels.verify_twilio_signature(token, url, params, "")


def test_meta_signature() -> None:
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert channels.verify_meta_signature("secret", body, sig)
    assert not channels.verify_meta_signature("secret", body + b" ", sig)
    assert not channels.verify_meta_signature("secret", body, sig[7:])
    assert not channels.verify_meta_signature("", body, sig)


def test_parse_whatsapp_messages() -> None:
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "15551112222", "profile": {"name": "Ann"}}],
        "messages": [
            {"from": "15551112222", "id": "wamid.A", "type": "text", "text": {"body": " hi "}},
            {"from": "15551112222", "id": "wamid.B", "type": "image"},
            {"from": "", "id": "wamid.C", "type": "text", "text": {"body": "x"}},
            {"from": "1999", "id": "wamid.D", "type": "text", "text": {"body": "  "}},
            {"from": "1888", "type": "text", "text": {"body": "yo"}},
        ]}}, {"value": {"statuses": [{"id": "s"}]}}]}, {"changes": None}]}
    assert channels.parse_whatsapp_messages(payload) == [
        {"from": "+15551112222", "id": "wamid.A", "text": "hi", "name": "Ann"},
        {"from": "+1888", "id": "", "text": "yo", "name": ""},
    ]
    assert channels.parse_whatsapp_messages({}) == []
