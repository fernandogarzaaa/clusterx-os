"""Messaging channels: outbound adapters and inbound webhook verification.

* ``simulator``: in-app chat; messages are only stored on the lead thread.
* ``twilio``: Twilio Programmable Messaging (SMS, or WhatsApp via a
  ``whatsapp:+...`` sender).
* ``whatsapp``: Meta WhatsApp Cloud API.

Adapters only talk to the provider; the caller records the message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from clusterx_os.config import Settings
from clusterx_os.integrations import ConfigurationError, IntegrationError, send

CHANNELS = ("simulator", "twilio", "whatsapp")


class ChannelAdapter(Protocol):
    name: str

    def send(self, to: str, text: str) -> str | None: ...

    def status(self) -> dict[str, Any]: ...


class SimulatorAdapter:
    name = "simulator"
    description = "In-app chat simulator. Messages appear on the lead thread. No credentials."

    def send(self, to: str, text: str) -> str | None:
        return None

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": True, "description": self.description}


def missing_settings(pairs: list[tuple[str, str]]) -> list[str]:
    return [label for label, value in pairs if not value]


class _CredentialedAdapter(ABC):
    name = ""
    description = ""

    @abstractmethod
    def check(self) -> None:
        """Raise ConfigurationError when credentials are missing."""

    def status(self) -> dict[str, Any]:
        try:
            self.check()
        except ConfigurationError as exc:
            return {"name": self.name, "configured": False, "description": self.description,
                    "detail": str(exc)}
        return {"name": self.name, "configured": True, "description": self.description,
                "detail": "Credentials present."}


class TwilioAdapter(_CredentialedAdapter):
    name = "twilio"
    description = ("Twilio Messages API (SMS / WhatsApp). Needs TWILIO_ACCOUNT_SID, "
                   "TWILIO_AUTH_TOKEN, TWILIO_FROM.")

    def __init__(self, sid: str, token: str, sender: str,
                 client: httpx.Client | None = None) -> None:
        self.sid, self.token, self.sender = sid, token, sender
        self._client = client or httpx.Client(timeout=20.0)

    def check(self) -> None:
        missing = missing_settings([("TWILIO_ACCOUNT_SID", self.sid),
                                    ("TWILIO_AUTH_TOKEN", self.token),
                                    ("TWILIO_FROM", self.sender)])
        if missing:
            raise ConfigurationError("Twilio adapter not configured. Set: " + ", ".join(missing))

    def send(self, to: str, text: str) -> str | None:
        self.check()
        if not to:
            raise IntegrationError("Cannot send via Twilio: lead has no phone number")
        recipient = to
        if self.sender.startswith("whatsapp:") and not to.startswith("whatsapp:"):
            recipient = f"whatsapp:{to}"
        resp = send("twilio", self._client, "POST",
                    f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                    auth=(self.sid, self.token),
                    data={"From": self.sender, "To": recipient, "Body": text})
        sid = resp.json().get("sid")
        return str(sid) if sid else None


class WhatsAppAdapter(_CredentialedAdapter):
    name = "whatsapp"
    description = ("WhatsApp Cloud API (graph.facebook.com). Needs WHATSAPP_TOKEN, "
                   "WHATSAPP_PHONE_NUMBER_ID.")

    def __init__(self, token: str, phone_number_id: str, api_version: str = "v21.0",
                 client: httpx.Client | None = None) -> None:
        self.token, self.phone_number_id, self.api_version = token, phone_number_id, api_version
        self._client = client or httpx.Client(timeout=20.0)

    def check(self) -> None:
        missing = missing_settings([("WHATSAPP_TOKEN", self.token),
                                    ("WHATSAPP_PHONE_NUMBER_ID", self.phone_number_id)])
        if missing:
            raise ConfigurationError("WhatsApp adapter not configured. Set: " + ", ".join(missing))

    def send(self, to: str, text: str) -> str | None:
        self.check()
        if not to:
            raise IntegrationError("Cannot send via WhatsApp: lead has no phone number")
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        resp = send("whatsapp", self._client, "POST", url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={"messaging_product": "whatsapp", "to": to.lstrip("+"),
                          "type": "text", "text": {"body": text}})
        messages = resp.json().get("messages") or [{}]
        mid = messages[0].get("id")
        return str(mid) if mid else None


def build_adapters(settings: Settings) -> dict[str, ChannelAdapter]:
    return {
        "simulator": SimulatorAdapter(),
        "twilio": TwilioAdapter(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN,
                                settings.TWILIO_FROM),
        "whatsapp": WhatsAppAdapter(settings.WHATSAPP_TOKEN, settings.WHATSAPP_PHONE_NUMBER_ID,
                                    settings.WHATSAPP_API_VERSION),
    }


def adapter_statuses(settings: Settings) -> list[dict[str, Any]]:
    return [a.status() for a in build_adapters(settings).values()]


# ------------------------------------------------------------- inbound ---

def twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Twilio request signature: base64(HMAC-SHA1(url + sorted key/value pairs))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio_signature(auth_token: str, url: str, params: Mapping[str, str],
                            signature: str) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)


def verify_meta_signature(app_secret: str, body: bytes, header: str) -> bool:
    """Validate Meta's ``X-Hub-Signature-256: sha256=<hex>`` header."""
    if not app_secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def parse_whatsapp_messages(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Extract inbound text messages from a WhatsApp Cloud API webhook payload."""
    out: list[dict[str, str]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = {
                c.get("wa_id", ""): (c.get("profile") or {}).get("name", "")
                for c in value.get("contacts", []) or []
            }
            for msg in value.get("messages", []) or []:
                if msg.get("type") != "text":
                    continue
                sender = str(msg.get("from", ""))
                body = str((msg.get("text") or {}).get("body", "")).strip()
                if not sender or not body:
                    continue
                out.append({"from": "+" + sender.lstrip("+"), "id": str(msg.get("id", "")),
                            "text": body, "name": names.get(sender, "")})
    return out
