from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import models
from app.core.db import get_db
from app.core.security import decode_token


def _token_from_request(request: Request) -> str | None:
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None


def page_user(request: Request, db: Session = Depends(get_db)) -> models.User | None:
    """Optional user for HTML pages; returns None when anonymous."""
    token = _token_from_request(request)
    if not token:
        return None
    email = decode_token(token)
    if not email:
        return None
    return db.query(models.User).filter(models.User.email == email).first()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Required user for JSON APIs; 401 when anonymous."""
    user = page_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def page_or_login(request: Request, db: Session = Depends(get_db)):
    """For page routes: returns User, or a RedirectResponse to /login."""
    user = page_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return user
