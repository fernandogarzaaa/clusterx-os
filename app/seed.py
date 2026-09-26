"""Seed runner. `python -m app.seed` creates the admin user and product demo data."""

from app import models
from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.core.security import get_password_hash


def run_seed() -> dict:
    init_db()
    db = SessionLocal()
    try:
        counts: dict = {"users": 0}
        admin = db.query(models.User).filter(models.User.email == settings.ADMIN_EMAIL).first()
        if not admin:
            db.add(
                models.User(
                    email=settings.ADMIN_EMAIL,
                    name="Admin",
                    hashed_password=get_password_hash(settings.ADMIN_PASSWORD),
                    role="admin",
                )
            )
            counts["users"] = 1
        product_counts: dict = {}
        try:
            from app.product_seed import seed_product

            product_counts = seed_product(db) or {}
        except ImportError:
            pass
        db.commit()
        counts.update(product_counts)
        print("SEED OK:", counts, flush=True)
        return counts
    finally:
        db.close()


def maybe_seed_on_startup() -> None:
    if settings.SEED_ON_STARTUP:
        run_seed()
        return
    db = SessionLocal()
    try:
        if db.query(models.User).count() == 0:
            db.close()
            run_seed()
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    run_seed()
