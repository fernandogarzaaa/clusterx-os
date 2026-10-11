"""Transactional outbox for external side effects (CRM sync and notes).

Jobs are written in the same transaction as the change that caused them and
executed by the worker with exponential backoff, so a CRM outage never loses
an update and never blocks a conversation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from clusterx_os.config import Settings
from clusterx_os.integrations import ConfigurationError, IntegrationError
from clusterx_os.integrations.crm import CRMAdapter, LeadSnapshot
from clusterx_os.models import Lead, OutboxJob, utcnow

logger = logging.getLogger(__name__)

CRM_SYNC = "crm.sync_lead"
CRM_NOTE = "crm.note"
MAX_BACKOFF_SECONDS = 3600


def backoff_seconds(attempts: int) -> int:
    """30s, 60s, 120s ... capped at one hour."""
    if attempts < 1:
        return 0
    return int(min(MAX_BACKOFF_SECONDS, 30 * 2 ** (attempts - 1)))


def enqueue(db: Session, kind: str, payload: dict[str, Any]) -> OutboxJob:
    job = OutboxJob(kind=kind, payload_json=json.dumps(payload), status="pending",
                    attempts=0, next_attempt_at=utcnow())
    db.add(job)
    return job


def has_pending(db: Session, kind: str, lead_id: int) -> bool:
    jobs = db.scalars(
        select(OutboxJob.payload_json).where(OutboxJob.kind == kind, OutboxJob.status == "pending")
    ).all()
    return any(json.loads(payload).get("lead_id") == lead_id for payload in jobs)


def enqueue_crm_sync(db: Session, settings: Settings, lead: Lead) -> bool:
    if settings.CRM_PROVIDER == "local":
        return False
    db.flush()
    if has_pending(db, CRM_SYNC, lead.id):
        return False
    enqueue(db, CRM_SYNC, {"lead_id": lead.id})
    return True


def enqueue_crm_note(db: Session, settings: Settings, lead: Lead, body: str) -> bool:
    if settings.CRM_PROVIDER == "local" or not body.strip():
        return False
    db.flush()
    enqueue(db, CRM_NOTE, {"lead_id": lead.id, "body": body})
    return True


def snapshot(lead: Lead) -> LeadSnapshot:
    return LeadSnapshot(id=lead.id, name=lead.name, email=lead.email, phone=lead.phone,
                        company=lead.company, stage=lead.stage, intent=lead.intent,
                        external_id=lead.crm_external_id)


def _sync(crm: CRMAdapter, lead: Lead, now: datetime) -> str:
    external_id = crm.sync_lead(snapshot(lead))
    lead.crm_external_id = external_id
    lead.crm_synced_at = now
    return external_id


def run_job(db: Session, crm: CRMAdapter, job: OutboxJob, now: datetime) -> None:
    payload = json.loads(job.payload_json or "{}")
    lead = db.get(Lead, payload.get("lead_id"))
    if lead is None:
        return  # lead deleted since; nothing to sync
    if job.kind == CRM_SYNC:
        _sync(crm, lead, now)
    elif job.kind == CRM_NOTE:
        external_id = lead.crm_external_id or _sync(crm, lead, now)
        crm.add_note(external_id, str(payload.get("body", "")))
    else:
        raise ConfigurationError(f"Unknown outbox job kind: {job.kind}")


def process_outbox(db: Session, settings: Settings, crm: CRMAdapter | None,
                   now: datetime | None = None, limit: int = 50) -> dict[str, int]:
    """Run due jobs. Commits after each job. Returns counts per outcome."""
    counts = {"done": 0, "retry": 0, "failed": 0}
    if crm is None:
        return counts
    now = now or utcnow()
    query = (select(OutboxJob)
             .where(OutboxJob.status == "pending", OutboxJob.next_attempt_at <= now)
             .order_by(OutboxJob.id).limit(limit))
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    for job in db.scalars(query).all():
        job.attempts += 1
        try:
            run_job(db, crm, job, now)
        except SQLAlchemyError:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the queue
            retryable = not isinstance(exc, ConfigurationError) and (
                not isinstance(exc, IntegrationError) or exc.retryable
            )
            if not isinstance(exc, IntegrationError | ConfigurationError):
                logger.exception("unexpected outbox error", extra={"job_id": job.id})
            job.last_error = str(exc)[:2000]
            if retryable and job.attempts < settings.OUTBOX_MAX_ATTEMPTS:
                job.next_attempt_at = now + timedelta(seconds=backoff_seconds(job.attempts))
                counts["retry"] += 1
            else:
                job.status = "failed"
                counts["failed"] += 1
            logger.warning("outbox job failed", extra={"job_id": job.id, "kind": job.kind,
                                                       "attempts": job.attempts,
                                                       "retryable": retryable})
        else:
            job.status = "done"
            job.last_error = ""
            counts["done"] += 1
        db.commit()
    return counts
