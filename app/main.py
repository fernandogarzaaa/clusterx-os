import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import models  # noqa: F401  (registers User + product models)
from app.auth import router as auth_router
from app.core.config import settings
from app.core.db import init_db
from app.seed import maybe_seed_on_startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    maybe_seed_on_startup()
    scheduler = None
    if os.environ.get("RUN_SCHEDULER") == "1":
        from app.scheduler import start_scheduler

        scheduler = start_scheduler()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.include_router(auth_router)
    # Product builder contract: define app/product.py with a FastAPI `router`.
    try:
        from app.product import router as product_router

        app.include_router(product_router)
    except ImportError:
        @app.get("/")
        def _root():
            return {"status": "ok", "detail": "product router not configured"}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": settings.APP_NAME}

    return app


app = create_app()
