"""CRM adapters.

The built-in CRM is the application database itself (leads, stages, events,
notes in the timeline), so ``CRM_PROVIDER=local`` needs no adapter.
``CRM_PROVIDER=hubspot`` mirrors every lead into HubSpot (free CRM) through
the CRM v3 API with a private-app access token. Calls run from the outbox
worker, so a HubSpot outage never blocks the conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from clusterx_os.config import Settings
from clusterx_os.integrations import ConfigurationError, IntegrationError, send

# HubSpot's default "Lead Status" (hs_lead_status) options.
HUBSPOT_LEAD_STATUS = {
    "New": "NEW",
    "Contacted": "CONNECTED",
    "Qualified": "IN_PROGRESS",
    "Booked": "OPEN_DEAL",
    "Won": "OPEN_DEAL",
    "Lost": "UNQUALIFIED",
}
NOTE_TO_CONTACT_ASSOCIATION = 202  # HUBSPOT_DEFINED note -> contact
_EXISTING_ID_RE = re.compile(r"Existing ID:\s*(\d+)")


@dataclass(frozen=True)
class LeadSnapshot:
    id: int
    name: str
    email: str
    phone: str
    company: str
    stage: str
    intent: str | None
    external_id: str | None


class CRMAdapter(Protocol):
    name: str

    def sync_lead(self, lead: LeadSnapshot) -> str: ...

    def add_note(self, external_id: str, body: str) -> str: ...

    def status(self) -> dict[str, Any]: ...


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def hubspot_properties(lead: LeadSnapshot) -> dict[str, str]:
    first, last = split_name(lead.name)
    if lead.intent == "Unqualified":
        status = "UNQUALIFIED"
    else:
        status = HUBSPOT_LEAD_STATUS.get(lead.stage, "NEW")
    props = {
        "firstname": first,
        "lastname": last,
        "email": lead.email,
        "phone": lead.phone,
        "company": lead.company,
        "hs_lead_status": status,
    }
    if lead.stage == "Won":
        props["lifecyclestage"] = "customer"
    return {k: v for k, v in props.items() if v}


class HubSpotCRM:
    name = "hubspot"

    def __init__(
        self,
        access_token: str,
        base_url: str = "https://api.hubapi.com",
        client: httpx.Client | None = None,
    ) -> None:
        if not access_token:
            raise ConfigurationError("HubSpot adapter needs HUBSPOT_ACCESS_TOKEN")
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=20.0)
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return send("hubspot", self._client, method, f"{self.base_url}{path}",
                    headers=self._headers, **kwargs)

    def find_contact_by_email(self, email: str) -> str | None:
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email"],
            "limit": 1,
        }
        resp = self._call("POST", "/crm/v3/objects/contacts/search", json=body)
        results = resp.json().get("results", [])
        return str(results[0]["id"]) if results else None

    def _create(self, props: dict[str, str]) -> str:
        try:
            resp = self._call("POST", "/crm/v3/objects/contacts", json={"properties": props})
        except IntegrationError as exc:
            match = _EXISTING_ID_RE.search(str(exc))
            if exc.status_code == 409 and match:
                existing = match.group(1)
                self._update(existing, props)
                return existing
            raise
        return str(resp.json()["id"])

    def _update(self, contact_id: str, props: dict[str, str]) -> None:
        self._call("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": props})

    def sync_lead(self, lead: LeadSnapshot) -> str:
        if not lead.email and not lead.phone:
            raise IntegrationError("HubSpot sync needs an email or phone", retryable=False)
        props = hubspot_properties(lead)
        contact_id = lead.external_id
        if not contact_id and lead.email:
            contact_id = self.find_contact_by_email(lead.email)
        if contact_id:
            try:
                self._update(contact_id, props)
            except IntegrationError as exc:
                if exc.status_code != 404:
                    raise
            else:
                return contact_id
        return self._create(props)

    def add_note(self, external_id: str, body: str) -> str:
        payload = {
            "properties": {
                "hs_note_body": body[:65000],
                "hs_timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            "associations": [
                {
                    "to": {"id": external_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": NOTE_TO_CONTACT_ASSOCIATION,
                        }
                    ],
                }
            ],
        }
        return str(self._call("POST", "/crm/v3/objects/notes", json=payload).json()["id"])

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": True, "base_url": self.base_url}


def get_crm(settings: Settings) -> CRMAdapter | None:
    """The external CRM adapter, or None when the built-in CRM is used."""
    if settings.CRM_PROVIDER == "hubspot":
        return HubSpotCRM(settings.HUBSPOT_ACCESS_TOKEN, settings.HUBSPOT_BASE_URL)
    return None


def crm_status(settings: Settings) -> dict[str, Any]:
    if settings.CRM_PROVIDER == "hubspot":
        return {"name": "hubspot", "configured": bool(settings.HUBSPOT_ACCESS_TOKEN),
                "detail": "Leads mirror to HubSpot via the outbox worker."}
    return {"name": "local", "configured": True,
            "detail": "Built-in CRM: pipeline, timeline and notes live in the app database."}
