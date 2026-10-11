"""Health checks, integration status, co-pilot."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from clusterx_os import __version__, copilot
from clusterx_os.config import Settings
from clusterx_os.db import get_db
from clusterx_os.integrations.calendars import calendar_status
from clusterx_os.integrations.channels import adapter_statuses
from clusterx_os.integrations.crm import crm_status
from clusterx_os.models import OutboxJob, User
from clusterx_os.web.deps import Integrations, current_user, integrations_dep, settings_dep
from clusterx_os.web.schemas import CopilotIn

router = APIRouter()


def db_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True


@router.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readiness(db: Session = Depends(get_db)) -> JSONResponse:
    ok = db_ok(db)
    return JSONResponse({"status": "ok" if ok else "unavailable", "database": ok},
                        status_code=200 if ok else 503)


@router.get("/api/health")
def health(db: Session = Depends(get_db),
           settings: Settings = Depends(settings_dep)) -> JSONResponse:
    ok = db_ok(db)
    return JSONResponse({"status": "ok" if ok else "degraded", "app": settings.APP_NAME,
                         "version": __version__, "database": ok},
                        status_code=200 if ok else 503)


@router.get("/api/channels")
def channels_api(settings: Settings = Depends(settings_dep),
                 _user: User = Depends(current_user)) -> dict[str, Any]:
    return {"adapters": adapter_statuses(settings), "outbound_default": settings.OUTBOUND_CHANNEL}


@router.get("/api/integrations")
def integrations_api(db: Session = Depends(get_db), settings: Settings = Depends(settings_dep),
                     integrations: Integrations = Depends(integrations_dep),
                     _user: User = Depends(current_user)) -> dict[str, Any]:
    llm = (integrations.llm.status() if integrations.llm
           else {"name": "offline", "configured": True, "detail": "Deterministic rule engine."})
    rows = db.execute(select(OutboxJob.status, func.count(OutboxJob.id))
                      .group_by(OutboxJob.status)).all()
    return {"llm": llm, "crm": crm_status(settings), "calendar": calendar_status(settings),
            "channels": adapter_statuses(settings),
            "outbox": {str(status): int(count) for status, count in rows},
            "config_problems": settings.problems()}


@router.post("/api/copilot")
def copilot_ask(payload: CopilotIn, db: Session = Depends(get_db),
                _user: User = Depends(current_user)) -> dict[str, Any]:
    return copilot.answer_question(db, payload.question)
