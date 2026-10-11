"""``clusterx-os`` command line."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Sequence

from clusterx_os import __version__


def _serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    from clusterx_os.config import get_settings, reset_settings
    from clusterx_os.logging_config import configure_logging
    from clusterx_os.web.app import StartupError, startup

    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    # Migrate, bootstrap and seed once here, before uvicorn forks: with several
    # workers each would otherwise race to create the schema on a fresh database.
    try:
        startup(settings)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    os.environ["AUTO_MIGRATE"] = "0"
    os.environ["SEED_DEMO_DATA"] = "0"
    reset_settings()
    uvicorn.run("clusterx_os.web.app:create_app", factory=True,
                host=args.host or settings.HOST, port=args.port or settings.PORT,
                workers=args.workers, proxy_headers=True,
                forwarded_allow_ips=args.forwarded_allow_ips, log_config=None)
    return 0


def _worker(args: argparse.Namespace) -> int:
    from clusterx_os.config import get_settings
    from clusterx_os.logging_config import configure_logging
    from clusterx_os.worker import Worker, tick

    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    if args.once:
        print(json.dumps(tick(settings), default=str))
        return 0
    worker = Worker(settings)
    worker.install_signal_handlers()
    worker.run_forever()
    return 0


def _migrate(args: argparse.Namespace) -> int:
    from clusterx_os.migrate import downgrade, upgrade

    if args.downgrade:
        downgrade(args.downgrade)
    else:
        upgrade(args.revision)
    print("migrations applied")
    return 0


def _seed_demo(_args: argparse.Namespace) -> int:
    from clusterx_os.config import get_settings
    from clusterx_os.migrate import upgrade
    from clusterx_os.seed import bootstrap_admin, seed_demo

    settings = get_settings()
    upgrade()
    bootstrap_admin(settings)
    print(json.dumps(seed_demo(settings)))
    return 0


def _create_user(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from clusterx_os.db import SessionLocal
    from clusterx_os.models import User
    from clusterx_os.security import hash_password, validate_password_strength

    password = args.password or getpass.getpass("Password: ")
    try:
        validate_password_strength(password)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    email = args.email.strip().lower()
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email)):
            print(f"error: user {email} already exists", file=sys.stderr)
            return 1
        db.add(User(email=email, name=args.name, role=args.role, is_active=True,
                    hashed_password=hash_password(password)))
        db.commit()
    print(f"created {args.role} {email}")
    return 0


def _check_config(_args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from clusterx_os.config import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"invalid configuration:\n{exc}", file=sys.stderr)
        return 1
    problems = settings.problems()
    for problem in problems:
        print(f"problem: {problem}")
    print(f"env={settings.APP_ENV} llm={settings.LLM_PROVIDER} crm={settings.CRM_PROVIDER} "
          f"calendar={settings.CALENDAR_PROVIDER} channel={settings.OUTBOUND_CHANNEL}")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clusterx-os", description="ClusterX OS")
    parser.add_argument("--version", action="version", version=f"clusterx-os {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web/API server")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    p.set_defaults(func=_serve)

    p = sub.add_parser("worker", help="run follow-ups, CRM outbox and housekeeping")
    p.add_argument("--once", action="store_true", help="run a single tick and exit")
    p.set_defaults(func=_worker)

    p = sub.add_parser("migrate", help="apply database migrations")
    p.add_argument("--revision", default="head")
    p.add_argument("--downgrade", default=None, metavar="REV")
    p.set_defaults(func=_migrate)

    p = sub.add_parser("seed-demo", help="load demo leads, conversations and a campaign")
    p.set_defaults(func=_seed_demo)

    p = sub.add_parser("create-user", help="create a dashboard/API user")
    p.add_argument("email")
    p.add_argument("--name", default="")
    p.add_argument("--role", choices=["admin", "member"], default="member")
    p.add_argument("--password", default=None, help="omit to be prompted")
    p.set_defaults(func=_create_user)

    p = sub.add_parser("check-config", help="validate environment configuration")
    p.set_defaults(func=_check_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
