"""Helpers shared by route modules."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from clusterx_os import idempotency
from clusterx_os.models import Lead

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_lead_or_404(db: Session, lead_id: int) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def idempotent(
    request: Request,
    db: Session,
    scope: str,
    payload: Any,
    handler: Callable[[], tuple[int, Any]],
) -> JSONResponse:
    """Run ``handler`` once per ``Idempotency-Key``; replay the stored result after."""
    raw_key = request.headers.get("Idempotency-Key")
    if raw_key is None:
        status_code, body = handler()
        return JSONResponse(body, status_code=status_code)
    try:
        key = idempotency.validate_key(raw_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fingerprint = idempotency.request_fingerprint(payload)
    try:
        stored = idempotency.lookup(db, scope, key, fingerprint)
    except idempotency.IdempotencyConflictError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if stored is not None:
        return JSONResponse(stored.body, status_code=stored.status_code,
                            headers={"Idempotent-Replayed": "true"})
    status_code, body = handler()
    idempotency.store(db, scope, key, fingerprint, status_code, body)
    return JSONResponse(body, status_code=status_code)
