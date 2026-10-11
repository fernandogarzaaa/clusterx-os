"""Application factory: middleware, routers, lifespan (migrations, bootstrap, worker)."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from clusterx_os import __version__
from clusterx_os.config import Settings, get_settings
from clusterx_os.integrations.calendars import get_calendar
from clusterx_os.integrations.channels import build_adapters
from clusterx_os.integrations.llm import get_llm
from clusterx_os.logging_config import configure_logging, request_id_var
from clusterx_os.web.common import STATIC_DIR
from clusterx_os.web.deps import Integrations
from clusterx_os.web.routes import auth, calendar, campaigns, leads, pages, system, webhooks

logger = logging.getLogger("clusterx_os.http")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
}
QUIET_PATHS = ("/healthz", "/readyz")


class StartupError(RuntimeError):
    """Configuration is unsafe for the selected environment."""


def build_integrations(settings: Settings) -> Integrations:
    return Integrations(llm=get_llm(settings), calendar=get_calendar(settings),
                        channels=build_adapters(settings))


def startup(settings: Settings) -> None:
    """Validate config, migrate, bootstrap the admin, seed demo data if asked."""
    from clusterx_os.migrate import upgrade
    from clusterx_os.seed import bootstrap_admin, seed_demo

    problems = settings.problems()
    for problem in problems:
        logger.warning("configuration problem", extra={"problem": problem})
    if problems and settings.is_production:
        raise StartupError("Refusing to start in production: " + "; ".join(problems))
    if settings.AUTO_MIGRATE:
        upgrade()
    bootstrap_admin(settings)
    if settings.SEED_DEMO_DATA:
        seed_demo(settings)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        startup(settings)
        worker = None
        if settings.RUN_SCHEDULER:
            from clusterx_os.worker import Worker

            worker = Worker(settings)
            worker.start_background()
        logger.info("started", extra={"version": __version__, "env": settings.APP_ENV})
        yield
        if worker is not None:
            worker.stop()

    app = FastAPI(title=settings.APP_NAME, version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.integrations = build_integrations(settings)
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    max_body = settings.MAX_UPLOAD_BYTES + 64_000

    @app.middleware("http")
    async def request_context(request: Request,
                              call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > max_body:
                response: Response = JSONResponse({"detail": "Request body too large"},
                                                  status_code=413)
            else:
                response = await call_next(request)
        except Exception:
            logger.exception("unhandled error", extra={"path": request.url.path})
            response = JSONResponse({"detail": "Internal server error", "request_id": rid},
                                    status_code=500)
        response.headers["X-Request-ID"] = rid
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if settings.COOKIE_SECURE:
            response.headers.setdefault("Strict-Transport-Security",
                                        "max-age=31536000; includeSubDomains")
        if request.url.path not in QUIET_PATHS:
            logger.info("request", extra={
                "method": request.method, "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
        request_id_var.reset(token)
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for module in (system, auth, leads, webhooks, calendar, campaigns, pages):
        app.include_router(module.router)
    return app
