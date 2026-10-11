"""Inbound webhooks: lead intake, generic messages, Twilio and WhatsApp."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from clusterx_os.config import Settings
from clusterx_os.db import get_db
from clusterx_os.integrations.channels import (
    parse_whatsapp_messages,
    verify_meta_signature,
    verify_twilio_signature,
)
from clusterx_os.security import constant_time_equals
from clusterx_os.services import leads as lead_service
from clusterx_os.web.common import get_lead_or_404, idempotent
from clusterx_os.web.deps import (
    Integrations,
    integrations_dep,
    rate_limit,
    require_webhook_key,
    settings_dep,
)
from clusterx_os.web.schemas import WebhookLeadIn, WebhookMessageIn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks",
                   dependencies=[Depends(rate_limit("webhook", "WEBHOOK_RATE_LIMIT_PER_MINUTE"))])

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@router.post("/leads", status_code=201, dependencies=[Depends(require_webhook_key)])
def webhook_lead(payload: WebhookLeadIn, request: Request, db: Session = Depends(get_db),
                 settings: Settings = Depends(settings_dep),
                 integrations: Integrations = Depends(integrations_dep)) -> JSONResponse:
    """Create or update a lead (deduplicated on email, then phone).

    Send ``Idempotency-Key`` to make retries safe. An optional ``message``
    starts the conversation and returns the agent's first reply.
    """

    def handler() -> tuple[int, Any]:
        data = payload.model_dump(exclude={"message", "source"})
        try:
            lead, created = lead_service.upsert_lead(db, settings, data,
                                                     payload.source or "webhook",
                                                     event_kind="webhook_received")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.commit()
        body: dict[str, Any] = {"lead": lead_service.serialize_lead(lead), "created": created}
        if payload.message:
            outcome = lead_service.handle_inbound(
                db, settings, lead, payload.message, "api", llm=integrations.llm,
                calendar=integrations.calendar, adapters=integrations.channels)
            body["reply"] = outcome.reply
            body["lead"] = lead_service.serialize_lead(lead)
        return (201 if created else 200), body

    return idempotent(request, db, "webhooks.leads", payload.model_dump(), handler)


@router.post("/messages", dependencies=[Depends(require_webhook_key)])
def webhook_message(payload: WebhookMessageIn, db: Session = Depends(get_db),
                    settings: Settings = Depends(settings_dep),
                    integrations: Integrations = Depends(integrations_dep)) -> dict[str, Any]:
    """A lead's message from any external system; returns the agent reply.

    Identify the lead with ``lead_id`` or ``email``/``phone`` (created if new).
    ``external_id`` deduplicates provider retries.
    """
    if payload.lead_id is not None:
        lead = get_lead_or_404(db, payload.lead_id)
    else:
        if not (payload.email or payload.phone):
            raise HTTPException(status_code=422, detail="Provide lead_id, email or phone")
        lead, _ = lead_service.upsert_lead(
            db, settings, {"name": payload.name, "email": payload.email, "phone": payload.phone},
            "api", event_kind="webhook_received")
    outcome = lead_service.handle_inbound(
        db, settings, lead, payload.text, "api", payload.external_id, llm=integrations.llm,
        calendar=integrations.calendar, adapters=integrations.channels)
    return {"lead_id": lead.id, "duplicate": outcome.duplicate, "reply": outcome.reply or None,
            "agent_stage": outcome.agent_stage or None}


def inbound_from_phone(db: Session, settings: Settings, integrations: Integrations,
                       channel: str, phone: str, text: str, external_id: str,
                       name: str = "") -> bool:
    """Route a provider message to its lead (created on first contact)."""
    try:
        normalized = lead_service.normalize_phone(phone)
    except ValueError:
        logger.warning("inbound message with invalid sender", extra={"channel": channel})
        return False
    if not normalized:
        return False
    lead, _ = lead_service.upsert_lead(db, settings, {"phone": normalized, "name": name},
                                       channel, event_kind="inbound_message")
    lead_service.handle_inbound(db, settings, lead, text, channel, external_id or None,
                                llm=integrations.llm, calendar=integrations.calendar,
                                adapters=integrations.channels)
    return True


def public_url(request: Request, settings: Settings) -> str:
    if settings.PUBLIC_BASE_URL:
        query = f"?{request.url.query}" if request.url.query else ""
        return settings.PUBLIC_BASE_URL.rstrip("/") + request.url.path + query
    return str(request.url)


@router.post("/twilio")
async def twilio_inbound(request: Request, db: Session = Depends(get_db),
                         settings: Settings = Depends(settings_dep),
                         integrations: Integrations = Depends(integrations_dep)) -> Response:
    """Twilio Messaging webhook (the number's 'A message comes in' URL)."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    if settings.TWILIO_VALIDATE_SIGNATURE and not verify_twilio_signature(
        settings.TWILIO_AUTH_TOKEN, public_url(request, settings), form,
        request.headers.get("X-Twilio-Signature", ""),
    ):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    body = form.get("Body", "").strip()
    if body:
        await run_in_threadpool(inbound_from_phone, db, settings, integrations, "twilio",
                                form.get("From", ""), body, form.get("MessageSid", ""),
                                form.get("ProfileName", ""))
    return Response(TWIML_EMPTY, media_type="application/xml")


@router.get("/whatsapp")
def whatsapp_verify(mode: str = Query("", alias="hub.mode"),
                    token: str = Query("", alias="hub.verify_token"),
                    challenge: str = Query("", alias="hub.challenge", max_length=256),
                    settings: Settings = Depends(settings_dep)) -> Response:
    """Meta webhook verification handshake."""
    if (mode == "subscribe" and settings.WHATSAPP_VERIFY_TOKEN
            and constant_time_equals(token, settings.WHATSAPP_VERIFY_TOKEN)):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
async def whatsapp_inbound(request: Request, db: Session = Depends(get_db),
                           settings: Settings = Depends(settings_dep),
                           integrations: Integrations = Depends(integrations_dep),
                           ) -> dict[str, Any]:
    raw = await request.body()
    if not verify_meta_signature(settings.WHATSAPP_APP_SECRET, raw,
                                 request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    messages = parse_whatsapp_messages(payload)
    for msg in messages:
        await run_in_threadpool(inbound_from_phone, db, settings, integrations, "whatsapp",
                                msg["from"], msg["text"], msg["id"], msg["name"])
    return {"received": len(messages)}
