"""Login, logout, current user and user administration."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from clusterx_os.config import Settings
from clusterx_os.db import get_db
from clusterx_os.models import User
from clusterx_os.security import (
    create_access_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from clusterx_os.web.common import templates
from clusterx_os.web.deps import COOKIE_NAME, current_user, rate_limit, require_admin, settings_dep
from clusterx_os.web.schemas import LoginIn, UserIn

logger = logging.getLogger(__name__)
router = APIRouter()
login_limit = rate_limit("login", "LOGIN_RATE_LIMIT_PER_MINUTE")

_dummy_hash: list[str] = []


def _dummy() -> str:
    """A hash checked when the email is unknown, so response timing reveals nothing."""
    if not _dummy_hash:
        _dummy_hash.append(hash_password("timing-equaliser-password-1"))
    return _dummy_hash[0]


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        verify_password(password, _dummy())
        return None
    if not verify_password(password, user.hashed_password) or not user.is_active:
        return None
    return user


def _set_cookie(resp: Response, token: str, settings: Settings) -> None:
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    secure=settings.COOKIE_SECURE,
                    max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


def serialize_user(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role,
            "is_active": user.is_active}


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "auth/login.html",
                                      {"error": request.query_params.get("error")})


@router.post("/login", dependencies=[Depends(login_limit)], include_in_schema=False)
def login_form(email: str = Form(..., max_length=255),
               password: str = Form(..., max_length=1024),
               db: Session = Depends(get_db),
               settings: Settings = Depends(settings_dep)) -> Response:
    user = authenticate(db, email, password)
    if user is None:
        logger.info("login failed")
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    _set_cookie(resp, create_access_token(user.email), settings)
    return resp


@router.post("/api/auth/login", dependencies=[Depends(login_limit)])
def login_api(payload: LoginIn, db: Session = Depends(get_db),
              settings: Settings = Depends(settings_dep)) -> Response:
    user = authenticate(db, payload.email, payload.password)
    if user is None:
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
    token = create_access_token(user.email)
    resp = JSONResponse({"access_token": token, "token_type": "bearer",
                         "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60})
    _set_cookie(resp, token, settings)
    return resp


@router.get("/logout", include_in_schema=False)
def logout_page() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.post("/api/auth/logout")
def logout_api() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/api/auth/me")
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return serialize_user(user)


@router.get("/api/users")
def list_users(db: Session = Depends(get_db),
               _admin: User = Depends(require_admin)) -> dict[str, Any]:
    return {"users": [serialize_user(u) for u in db.scalars(select(User).order_by(User.id))]}


@router.post("/api/users", status_code=201)
def create_user(payload: UserIn, db: Session = Depends(get_db),
                _admin: User = Depends(require_admin)) -> dict[str, Any]:
    try:
        validate_password_strength(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="A user with that email exists")
    user = User(email=payload.email, name=payload.name, role=payload.role, is_active=True,
                hashed_password=hash_password(payload.password))
    db.add(user)
    db.commit()
    return serialize_user(user)


@router.post("/api/users/{user_id}/deactivate")
def deactivate_user(user_id: int, db: Session = Depends(get_db),
                    admin: User = Depends(require_admin)) -> dict[str, Any]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
    user.is_active = False
    db.commit()
    return serialize_user(user)
