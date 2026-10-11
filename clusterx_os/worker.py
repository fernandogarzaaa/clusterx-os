"""Background worker: campaign follow-ups, CRM outbox, housekeeping.

Run it as its own process (``clusterx-os worker``) or, for a single-process
deployment, in a thread inside the API (``RUN_SCHEDULER=1``). Run exactly
one worker per database.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from clusterx_os import idempotency
from clusterx_os.config import Settings, get_settings
from clusterx_os.db import SessionLocal
from clusterx_os.integrations import ConfigurationError
from clusterx_os.integrations.crm import CRMAdapter, get_crm
from clusterx_os.services.followups import run_due_followups
from clusterx_os.services.outbox import process_outbox

logger = logging.getLogger(__name__)


def tick(settings: Settings | None = None, crm: CRMAdapter | None = None) -> dict[str, Any]:
    """One pass of every background job. Errors are logged, never raised."""
    settings = settings or get_settings()
    if crm is None:
        try:
            crm = get_crm(settings)
        except ConfigurationError as exc:
            logger.error("CRM adapter misconfigured; outbox paused", extra={"error": str(exc)})
    report: dict[str, Any] = {}
    jobs: list[tuple[str, Callable[[Session], Any]]] = [
        ("followups", lambda db: len(run_due_followups(db, settings))),
        ("outbox", lambda db: process_outbox(db, settings, crm)),
        ("idempotency_purged",
         lambda db: idempotency.purge_expired(db, settings.IDEMPOTENCY_TTL_HOURS)),
    ]
    for name, job in jobs:
        db = SessionLocal()
        try:
            report[name] = job(db)
        except Exception:  # noqa: BLE001 - keep the worker alive
            db.rollback()
            report[name] = "error"
            logger.exception("worker job failed", extra={"job": name})
        finally:
            db.close()
    logger.info("worker tick", extra={"report": report})
    return report


class Worker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run_forever(self) -> None:
        logger.info("worker started", extra={"interval": self.settings.SCHEDULER_INTERVAL_SECONDS})
        while not self._stop.is_set():
            tick(self.settings)
            self._stop.wait(self.settings.SCHEDULER_INTERVAL_SECONDS)
        logger.info("worker stopped")

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="clusterx-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: object) -> None:
            logger.info("signal received; stopping", extra={"signal": signum})
            self._stop.set()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
