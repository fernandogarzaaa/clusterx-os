"""Outbound channel adapters.

SimulatorAdapter is the default and always works: it records the outbound
message on the lead's thread (in-app chat).

TwilioAdapter and WhatsAppAdapter are real httpx implementations against
api.twilio.com and graph.facebook.com. They raise ConfigurationError with a
clear message when credentials are missing, and surface provider errors
honestly instead of pretending the send worked.
"""

from __future__ import annotations

import os
from typing import Protocol

from sqlalchemy.orm import Session

from app import models


class ConfigurationError(RuntimeError):
    """Raised when a channel adapter lacks the credentials it needs."""


class ChannelAdapter(Protocol):
    name: str

    def send(self, db: Session, lead: models.Lead, text: str) -> bool: ...
    def status(self) -> dict: ...


class SimulatorAdapter:
    name = "simulator"
    description = (
        "In-app chat simulator. Messages appear in the lead thread instantly. "
        "No credentials needed."
    )

    def send(self, db: Session, lead: models.Lead, text: str) -> bool:
        db.add(
            models.Message(
                lead_id=lead.id, role="agent", text=text, channel="simulator"
            )
        )
        db.flush()
        return True

    def status(self) -> dict:
        return {
            "name": self.name,
            "configured": True,
            "description": self.description,
        }


class TwilioAdapter:
    name = "twilio"
    description = (
        "Sends via the Twilio Messages API (SMS/WhatsApp). Needs "
        "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM set."
    )

    def _creds(self) -> tuple[str, str, str]:
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        sender = os.getenv("TWILIO_FROM", "")
        missing = [
            label
            for label, value in [
                ("TWILIO_ACCOUNT_SID", sid),
                ("TWILIO_AUTH_TOKEN", token),
                ("TWILIO_FROM", sender),
            ]
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Twilio adapter not configured. Set: " + ", ".join(missing)
            )
        return sid, token, sender

    def send(self, db: Session, lead: models.Lead, text: str) -> bool:
        import httpx

        sid, token, sender = self._creds()
        if not lead.phone:
            raise ConfigurationError(
                "Cannot send via Twilio: lead has no phone number."
            )
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"From": sender, "To": lead.phone, "Body": text},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Twilio send failed ({resp.status_code}): {resp.text[:200]}")
        db.add(
            models.Message(lead_id=lead.id, role="agent", text=text, channel="twilio")
        )
        db.flush()
        return True

    def status(self) -> dict:
        try:
            self._creds()
            configured = True
            detail = "Credentials present."
        except ConfigurationError as exc:
            configured = False
            detail = str(exc)
        return {"name": self.name, "configured": configured,
                "description": self.description, "detail": detail}


class WhatsAppAdapter:
    name = "whatsapp"
    description = (
        "Sends via the WhatsApp Cloud API (graph.facebook.com). Needs "
        "WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID set."
    )

    def _creds(self) -> tuple[str, str]:
        token = os.getenv("WHATSAPP_TOKEN", "")
        phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        missing = [
            label
            for label, value in [
                ("WHATSAPP_TOKEN", token),
                ("WHATSAPP_PHONE_NUMBER_ID", phone_id),
            ]
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "WhatsApp adapter not configured. Set: " + ", ".join(missing)
            )
        return token, phone_id

    def send(self, db: Session, lead: models.Lead, text: str) -> bool:
        import httpx

        token, phone_id = self._creds()
        if not lead.phone:
            raise ConfigurationError(
                "Cannot send via WhatsApp: lead has no phone number."
            )
        resp = httpx.post(
            f"https://graph.facebook.com/v21.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "to": lead.phone,
                "type": "text",
                "text": {"body": text},
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"WhatsApp send failed ({resp.status_code}): {resp.text[:200]}"
            )
        db.add(
            models.Message(lead_id=lead.id, role="agent", text=text, channel="whatsapp")
        )
        db.flush()
        return True

    def status(self) -> dict:
        try:
            self._creds()
            configured = True
            detail = "Credentials present."
        except ConfigurationError as exc:
            configured = False
            detail = str(exc)
        return {"name": self.name, "configured": configured,
                "description": self.description, "detail": detail}


_ADAPTERS: dict[str, ChannelAdapter] = {
    "simulator": SimulatorAdapter(),
    "twilio": TwilioAdapter(),
    "whatsapp": WhatsAppAdapter(),
}


def get_adapter(name: str = "simulator") -> ChannelAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ConfigurationError(f"Unknown channel adapter: {name!r}")


def adapter_statuses() -> list[dict]:
    return [adapter.status() for adapter in _ADAPTERS.values()]
