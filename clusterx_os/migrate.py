"""Run Alembic migrations programmatically (``clusterx-os migrate``)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from clusterx_os.config import get_settings
from clusterx_os.db import ensure_sqlite_dir

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", (url or get_settings().DATABASE_URL).replace("%", "%%"))
    return cfg


def upgrade(revision: str = "head", url: str | None = None) -> None:
    target = url or get_settings().DATABASE_URL
    ensure_sqlite_dir(target)
    command.upgrade(alembic_config(target), revision)


def downgrade(revision: str, url: str | None = None) -> None:
    command.downgrade(alembic_config(url), revision)
