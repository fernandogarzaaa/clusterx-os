"""FastAPI dependencies: settings, auth, API keys, rate limits, integrations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from clusterx_os.config import Settings, get_settings
from clusterx_os.db import get_db
from clusterx_os.integrations.calendars import CalendarAdapter
from clusterx_os.integrations.channels import ChannelAdapter
from clusterx_os.integrations.llm import LLMClient
from clusterx_os.models import User
from clusterx_os.ratelimit import limiter
from clusterx_os.security import constant_time_equals, decode_token

COOKIE_NAME = "access_token"


@dataclass
class Integrations:
    llm: LLMClient | None = None
    calendar: CalendarAdapter | None = None
    channels: dict[str, ChannelAdapter] = field(default_factory=dict)


def settings_dep(request: Request) -> Settings:
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return settings


def integrations_dep(request: Request) -> Integrations:
    integrations: Integrations = request.app.state.integrations
    return integrations


def _token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(COOKIE_NAME) or None


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = _token(request)
    if not token:
        return None
    email = decode_token(token)
    if not email:
        return None
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        return None
    return user


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def require_webhook_key(request: Request, settings: Settings = Depends(settings_dep)) -> None:
    key = request.headers.get("X-API-Key", "")
    if not key or not constant_time_equals(key, settings.WEBHOOK_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, setting: str) -> Callable[..., None]:
    """Dependency enforcing ``settings.<setting>`` requests per minute per client IP."""

    def _dep(request: Request, settings: Settings = Depends(settings_dep)) -> None:
        limit = int(getattr(settings, setting))
        allowed, retry_after = limiter.hit(f"{bucket}:{client_ip(request)}", limit)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                detail="Rate limit exceeded",
                                headers={"Retry-After": str(retry_after)})

    return _dep
