"""Runtime configuration. Every setting comes from an environment variable
(or a local ``.env`` file); see ``.env.example`` for the full list."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET_KEY = "dev-insecure-secret-key-change-me-in-production"  # noqa: S105
DEFAULT_WEBHOOK_KEY = "dev-webhook-key"
DEFAULT_ADMIN_PASSWORD = "ChangeMe123!"  # noqa: S105 - documented dev default, refused in prod


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    # --- app ---
    APP_ENV: Literal["development", "production", "test"] = "development"
    APP_NAME: str = "ClusterX OS"
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8005, ge=1, le=65535)
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "json"
    PUBLIC_BASE_URL: str = ""

    # --- database ---
    DATABASE_URL: str = "sqlite:///./data/app.db"
    AUTO_MIGRATE: bool = True

    # --- auth ---
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 12, ge=5)
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=16)
    COOKIE_SECURE: bool = False
    CORS_ORIGINS: str = ""
    ADMIN_EMAIL: str = "admin@clusterx.local"
    ADMIN_PASSWORD: str = DEFAULT_ADMIN_PASSWORD
    SEED_DEMO_DATA: bool = False

    # --- inbound webhooks / limits ---
    WEBHOOK_API_KEY: str = DEFAULT_WEBHOOK_KEY
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = Field(default=120, ge=1)
    LOGIN_RATE_LIMIT_PER_MINUTE: int = Field(default=10, ge=1)
    API_RATE_LIMIT_PER_MINUTE: int = Field(default=600, ge=1)
    MAX_UPLOAD_BYTES: int = Field(default=2_000_000, ge=1024)
    IDEMPOTENCY_TTL_HOURS: int = Field(default=24, ge=1)

    # --- calendar / booking ---
    BUSINESS_TIMEZONE: str = "UTC"
    SLOT_MINUTES: int = Field(default=30, ge=5, le=480)
    BOOKING_HORIZON_DAYS: int = Field(default=14, ge=1, le=365)
    CALENDAR_PROVIDER: Literal["local", "google", "calcom"] = "local"
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REFRESH_TOKEN: str = ""
    GOOGLE_CALENDAR_ID: str = "primary"
    GOOGLE_SEND_UPDATES: Literal["all", "externalOnly", "none"] = "none"
    CALCOM_API_KEY: str = ""
    CALCOM_EVENT_TYPE_ID: int = 0
    CALCOM_BASE_URL: str = "https://api.cal.com"

    # --- CRM ---
    CRM_PROVIDER: Literal["local", "hubspot"] = "local"
    HUBSPOT_ACCESS_TOKEN: str = ""
    HUBSPOT_BASE_URL: str = "https://api.hubapi.com"

    # --- LLM ---
    LLM_PROVIDER: Literal["offline", "mock", "openai"] = "offline"
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "llama-3.1-8b-instant"
    LLM_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    LLM_JSON_MODE: bool = True

    # --- background worker ---
    RUN_SCHEDULER: bool = False
    SCHEDULER_INTERVAL_SECONDS: int = Field(default=60, ge=5)
    OUTBOX_MAX_ATTEMPTS: int = Field(default=8, ge=1)

    # --- channels ---
    OUTBOUND_CHANNEL: Literal["simulator", "twilio", "whatsapp"] = "simulator"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM: str = ""
    TWILIO_VALIDATE_SIGNATURE: bool = True
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_APP_SECRET: str = ""
    WHATSAPP_API_VERSION: str = "v21.0"

    @field_validator("SECRET_KEY", "WEBHOOK_API_KEY", "ADMIN_PASSWORD", mode="before")
    @classmethod
    def _blank_means_default(cls, value: object, info: ValidationInfo) -> object:
        """An empty value (as in .env.example) means "not set": keep the documented default,
        which production refuses to start with."""
        if isinstance(value, str) and not value.strip():
            defaults = {"SECRET_KEY": DEFAULT_SECRET_KEY, "WEBHOOK_API_KEY": DEFAULT_WEBHOOK_KEY,
                        "ADMIN_PASSWORD": DEFAULT_ADMIN_PASSWORD}
            return defaults[str(info.field_name)]
        return value

    @field_validator("BUSINESS_TIMEZONE")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {value!r}") from exc
        return value

    @field_validator("LOG_LEVEL")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid LOG_LEVEL: {value!r}")
        return upper

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.BUSINESS_TIMEZONE)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def problems(self) -> list[str]:
        """Configuration problems. In production any problem aborts startup."""
        issues: list[str] = []
        if self.is_production:
            if self.SECRET_KEY == DEFAULT_SECRET_KEY or len(self.SECRET_KEY) < 32:
                issues.append("SECRET_KEY must be set to a random value of at least 32 characters")
            if self.WEBHOOK_API_KEY == DEFAULT_WEBHOOK_KEY or len(self.WEBHOOK_API_KEY) < 16:
                issues.append(
                    "WEBHOOK_API_KEY must be set to a random value of at least 16 characters"
                )
            if self.ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
                issues.append("ADMIN_PASSWORD must not be the default value")
            if self.SEED_DEMO_DATA:
                issues.append("SEED_DEMO_DATA must be off in production")
        if self.LLM_PROVIDER == "openai" and not self.LLM_BASE_URL:
            issues.append("LLM_PROVIDER=openai requires LLM_BASE_URL")
        if self.CRM_PROVIDER == "hubspot" and not self.HUBSPOT_ACCESS_TOKEN:
            issues.append("CRM_PROVIDER=hubspot requires HUBSPOT_ACCESS_TOKEN")
        if self.CALENDAR_PROVIDER == "google" and not (
            self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET and self.GOOGLE_REFRESH_TOKEN
        ):
            issues.append(
                "CALENDAR_PROVIDER=google requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET "
                "and GOOGLE_REFRESH_TOKEN"
            )
        if self.CALENDAR_PROVIDER == "calcom" and not (
            self.CALCOM_API_KEY and self.CALCOM_EVENT_TYPE_ID > 0
        ):
            issues.append(
                "CALENDAR_PROVIDER=calcom requires CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID"
            )
        return issues


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Drop the cached settings (tests and CLI use this after changing env)."""
    get_settings.cache_clear()
