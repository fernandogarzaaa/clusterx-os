"""CLI, worker, seed, migrations and logging."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from clusterx_os import __version__, cli, worker
from clusterx_os.config import Settings, reset_settings
from clusterx_os.logging_config import JsonFormatter, configure_logging, request_id_var
from clusterx_os.migrate import downgrade, upgrade
from clusterx_os.models import Appointment, Campaign, Lead, OutboxJob, User
from clusterx_os.seed import bootstrap_admin, seed_demo
from tests.test_services import FakeCRM


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"clusterx-os {__version__}"


def test_cli_requires_command() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_cli_check_config(capsys: pytest.CaptureFixture[str],
                          monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["check-config"]) == 0
    assert "env=test llm=offline crm=local calendar=local channel=simulator" in \
        capsys.readouterr().out
    monkeypatch.setenv("CRM_PROVIDER", "hubspot")
    reset_settings()
    assert cli.main(["check-config"]) == 1
    assert "HUBSPOT_ACCESS_TOKEN" in capsys.readouterr().out
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Nowhere/Land")
    reset_settings()
    assert cli.main(["check-config"]) == 1
    assert "invalid configuration" in capsys.readouterr().err


def test_cli_serve_migrates_once_before_forking(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from clusterx_os.web import app as web_app

    calls: list[str] = []
    seen: dict[str, Any] = {}

    def fake_startup(settings: Settings) -> None:
        calls.append(f"startup migrate={settings.AUTO_MIGRATE}")

    def fake_run(target: str, **kwargs: Any) -> None:
        calls.append("run")
        seen.update(kwargs, env_migrate=os.environ["AUTO_MIGRATE"],
                    env_seed=os.environ["SEED_DEMO_DATA"])

    monkeypatch.setenv("AUTO_MIGRATE", "1")
    monkeypatch.setenv("SEED_DEMO_DATA", "1")
    reset_settings()
    monkeypatch.setattr(web_app, "startup", fake_startup)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["serve", "--port", "9123", "--workers", "3"]) == 0
    # Startup ran once in the parent; workers are told not to migrate or seed again.
    assert calls == ["startup migrate=True", "run"]
    assert seen["env_migrate"] == "0" and seen["env_seed"] == "0"
    assert seen["port"] == 9123 and seen["workers"] == 3 and seen["factory"] is True
    reset_settings()


def test_cli_serve_refuses_bad_production_config(monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    import uvicorn

    def fail_run(*_a: Any, **_k: Any) -> None:
        raise AssertionError("uvicorn must not start")

    monkeypatch.setattr(uvicorn, "run", fail_run)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "short")
    reset_settings()
    assert cli.main(["serve"]) == 1
    assert "Refusing to start in production" in capsys.readouterr().err
    reset_settings()


def test_cli_create_user(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["create-user", "New@X.example", "--password", "short"]) == 2
    assert cli.main(["create-user", "New@X.example", "--password", "good-password-1",
                     "--role", "admin", "--name", "N"]) == 0
    assert "created admin new@x.example" in capsys.readouterr().out
    user = db.scalars(select(User).where(User.email == "new@x.example")).one()
    assert (user.role, user.name, user.is_active) == ("admin", "N", True)
    assert cli.main(["create-user", "new@x.example", "--password", "good-password-1"]) == 1


def test_cli_worker_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["worker", "--once"]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report == {"followups": 0, "outbox": {"done": 0, "retry": 0, "failed": 0},
                      "idempotency_purged": 0}


def test_cli_migrate_and_seed(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    assert "migrations applied" in capsys.readouterr().out
    assert cli.main(["seed-demo"]) == 0
    counts = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert counts == {"leads": 8, "appointments": 1, "campaigns": 1}


def test_worker_tick_runs_outbox_and_survives_errors(db: Session, settings: Settings,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings.model_copy(update={"CRM_PROVIDER": "hubspot", "HUBSPOT_ACCESS_TOKEN": "t"})
    from clusterx_os.services import leads

    leads.create_lead(db, s, {"name": "A", "email": "a@x.example"}, "manual")
    db.commit()
    report = worker.tick(s, FakeCRM())
    assert report["outbox"] == {"done": 1, "retry": 0, "failed": 0}

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("followups broke")

    monkeypatch.setattr(worker, "run_due_followups", boom)
    report = worker.tick(s, FakeCRM())
    assert report["followups"] == "error"
    assert report["idempotency_purged"] == 0


def test_worker_tick_with_misconfigured_crm(settings: Settings) -> None:
    s = settings.model_copy(update={"CRM_PROVIDER": "hubspot", "HUBSPOT_ACCESS_TOKEN": ""})
    assert worker.tick(s)["outbox"] == {"done": 0, "retry": 0, "failed": 0}


def test_worker_loop_start_stop(settings: Settings) -> None:
    w = worker.Worker(settings.model_copy(update={"SCHEDULER_INTERVAL_SECONDS": 5}))
    assert w.stopped is False
    w.start_background()
    w.stop(timeout=5)
    assert w.stopped is True


def test_worker_signal_handlers(settings: Settings) -> None:
    import signal

    w = worker.Worker(settings)
    old_term, old_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        w.install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert w.stopped
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def test_seed_is_idempotent(db: Session, settings: Settings) -> None:
    assert bootstrap_admin(settings) is False  # conftest already bootstrapped
    first = seed_demo(settings)
    assert first == {"leads": 8, "appointments": 1, "campaigns": 1}
    assert seed_demo(settings) == {"leads": 0, "appointments": 0, "campaigns": 0}
    assert len(db.scalars(select(Lead)).all()) == 8
    hot = db.scalars(select(Lead).where(Lead.email == "aarav@acme.example")).one()
    assert (hot.intent, hot.stage) == ("Hot", "Qualified")
    meera = db.scalars(select(Lead).where(Lead.email == "meera@healthplus.example")).one()
    assert meera.opted_out is True
    assert db.scalars(select(Appointment)).one().status == "confirmed"
    assert db.scalars(select(Campaign)).one().name == "No-reply follow-up"
    assert db.scalars(select(OutboxJob)).all() == []


def test_migrations_up_down_up() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'nested' / 'm.db'}"
        upgrade(url=url)
        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names())
        assert {"leads", "messages", "appointments", "outbox_jobs", "idempotency_records",
                "alembic_version"} <= tables
        indexes = {i["name"] for i in inspect(engine).get_indexes("appointments")}
        assert "uq_appointment_confirmed_start" in indexes
        downgrade("base", url=url)
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        upgrade(url=url)
        assert "leads" in set(inspect(engine).get_table_names())
        engine.dispose()


def test_models_match_migrations() -> None:
    """Autogenerate against a migrated DB finds no differences."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from clusterx_os.db import Base

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'cmp.db'}"
        upgrade(url=url)
        engine = create_engine(url)
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        engine.dispose()
    assert diff == []


def test_json_logging() -> None:
    fmt = JsonFormatter()
    record = logging.makeLogRecord({"name": "x", "levelname": "INFO", "msg": "hello %s",
                                    "args": ("world",), "lead_id": 5})
    token = request_id_var.set("rid-1")
    try:
        data = json.loads(fmt.format(record))
    finally:
        request_id_var.reset(token)
    assert data["msg"] == "hello world"
    assert data["lead_id"] == 5 and data["request_id"] == "rid-1"
    assert data["level"] == "INFO" and data["logger"] == "x"
    try:
        raise ValueError("bad")
    except ValueError:
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "oops", None, sys.exc_info())
    assert "ValueError: bad" in json.loads(fmt.format(rec))["exc"]
    assert "request_id" not in json.loads(fmt.format(logging.makeLogRecord({"msg": "m"})))


def test_configure_logging_modes() -> None:
    configure_logging("DEBUG", "json")
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.DEBUG
    configure_logging("WARNING", "text")
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    stream = io.StringIO()
    root.handlers[0].stream = stream
    logging.getLogger("t").warning("visible")
    assert "visible" in stream.getvalue()


def test_module_entrypoint() -> None:
    env = {**os.environ}
    out = subprocess.run([sys.executable, "-m", "clusterx_os", "--version"], capture_output=True,
                         text=True, env=env, check=False, timeout=60)
    assert out.returncode == 0
    assert out.stdout.strip() == f"clusterx-os {__version__}"
