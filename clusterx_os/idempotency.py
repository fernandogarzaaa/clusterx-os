"""Idempotency keys for POST endpoints (``Idempotency-Key`` header).

A repeated request with the same key and the same body replays the stored
response; the same key with a different body is rejected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clusterx_os.models import IdempotencyRecord, utcnow

MAX_KEY_LENGTH = 255


class IdempotencyConflictError(Exception):
    """Key reused with a different request body."""


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: Any


def request_fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_key(key: str) -> str:
    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError(f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} characters")
    return key


def lookup(db: Session, scope: str, key: str, fingerprint: str) -> StoredResponse | None:
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope, IdempotencyRecord.key == key
        )
    )
    if record is None:
        return None
    if record.request_hash != fingerprint:
        raise IdempotencyConflictError(
            "Idempotency-Key was already used with a different request body"
        )
    return StoredResponse(record.status_code, json.loads(record.response_json))


def store(
    db: Session, scope: str, key: str, fingerprint: str, status_code: int, body: Any
) -> bool:
    """Persist the response and commit. Returns False if a concurrent request won."""
    db.add(
        IdempotencyRecord(
            scope=scope,
            key=key,
            request_hash=fingerprint,
            status_code=status_code,
            response_json=json.dumps(body, default=str),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def purge_expired(db: Session, ttl_hours: int, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - timedelta(hours=ttl_hours)
    stale = db.scalars(
        select(IdempotencyRecord.id).where(IdempotencyRecord.created_at < cutoff)
    ).all()
    if stale:
        db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.id.in_(stale)))
    db.commit()
    return len(stale)
