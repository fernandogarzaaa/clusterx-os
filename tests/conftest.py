"""Shared fixtures. The app runs fully offline: SQLite, rule-engine LLM, local CRM/calendar."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_TMP = Path(tempfile.mkdtemp(prefix="clusterx-test-"))
os.environ.update({
    "APP_ENV": "test",
    # TEST_DATABASE_URL runs the suite against another database (CI uses Postgres 16).
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'test.db'}",
    "AUTO_MIGRATE": "0",
    "BCRYPT_ROUNDS": "4",
    "LOG_FORMAT": "text",
    "LOG_LEVEL": "WARNING",
    "SECRET_KEY": "test-secret-key-that-is-long-enough-0123456789",
    "WEBHOOK_API_KEY": "test-webhook-key-123456",
    "ADMIN_EMAIL": "admin@clusterx.local",
    "ADMIN_PASSWORD": "ChangeMe123!",
    "BUSINESS_TIMEZONE": "UTC",
    "CRM_PROVIDER": "local",
    "CALENDAR_PROVIDER": "local",
    "LLM_PROVIDER": "offline",
    "OUTBOUND_CHANNEL": "simulator",
    "SEED_DEMO_DATA": "0",
    "RUN_SCHEDULER": "0",
})
for _key in ("HUBSPOT_ACCESS_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM",
             "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "LLM_BASE_URL", "LLM_API_KEY"):
    os.environ.pop(_key, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from clusterx_os.config import Settings, get_settings, reset_settings  # noqa: E402
from clusterx_os.db import Base, SessionLocal, reset_engine  # noqa: E402
from clusterx_os.migrate import upgrade  # noqa: E402
from clusterx_os.ratelimit import limiter  # noqa: E402
from clusterx_os.seed import bootstrap_admin  # noqa: E402

ADMIN = {"email": "admin@clusterx.local", "password": "ChangeMe123!"}


@pytest.fixture(scope="session", autouse=True)
def _migrated() -> Iterator[None]:
    reset_settings()
    reset_engine()
    upgrade()
    yield
    reset_engine()


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Every test starts with an empty database (plus the bootstrap admin)."""
    reset_settings()
    limiter.reset()
    with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
        db.commit()
    bootstrap_admin(get_settings())
    yield


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_client(**overrides: Any) -> TestClient:
    from clusterx_os.web.app import create_app

    settings = get_settings().model_copy(update=overrides)
    return TestClient(create_app(settings))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as c:
        yield c


@pytest.fixture
def auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def webhook_key() -> dict[str, str]:
    return {"X-API-Key": "test-webhook-key-123456"}
