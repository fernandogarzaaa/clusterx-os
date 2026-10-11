"""Request schemas with validation."""

from __future__ import annotations

from typing import Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, field_validator

from clusterx_os.services.leads import normalize_phone

Stage = Literal["New", "Contacted", "Qualified", "Booked", "Won", "Lost"]
_SOURCE = r"^[A-Za-z0-9_.:-]*$"


def check_email(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError(f"Invalid email: {exc}") from exc


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Contact(BaseModel):
    """Shared email/phone validation."""

    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=32)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        return check_email(v)

    @field_validator("phone")
    @classmethod
    def _valid_phone(cls, v: str) -> str:
        return normalize_phone(v)


class LeadIn(_Contact):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="", max_length=255)
    company: str = Field(default="", max_length=255)
    source: str = Field(default="manual", max_length=64, pattern=_SOURCE)


class WebhookLeadIn(_Contact):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    name: str = Field(default="", max_length=255)
    company: str = Field(default="", max_length=255)
    source: str = Field(default="webhook", max_length=64, pattern=_SOURCE)
    message: str = Field(default="", max_length=4000)


class WebhookMessageIn(_Contact):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=4000)
    lead_id: int | None = None
    name: str = Field(default="", max_length=255)
    external_id: str | None = Field(default=None, max_length=128)


class LeadPatch(_Strict):
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    company: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str | None) -> str | None:
        return None if v is None else check_email(v)

    @field_validator("phone")
    @classmethod
    def _valid_phone(cls, v: str | None) -> str | None:
        return None if v is None else normalize_phone(v)


class StageIn(_Strict):
    stage: Stage


class MessageIn(_Strict):
    text: str = Field(min_length=1, max_length=4000)


class AppointmentIn(_Strict):
    lead_id: int
    starts_at: str = Field(max_length=64, description="ISO-8601 datetime; naive values are UTC")


class CampaignIn(_Strict):
    name: str = Field(min_length=1, max_length=255)
    template_text: str = Field(min_length=1, max_length=2000)
    followup_hours: int = Field(default=24, ge=1, le=24 * 30)
    max_followups: int = Field(default=3, ge=1, le=20)


class CampaignPatch(_Strict):
    active: bool | None = None
    template_text: str | None = Field(default=None, min_length=1, max_length=2000)
    followup_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    max_followups: int | None = Field(default=None, ge=1, le=20)


class EnrollIn(_Strict):
    lead_ids: list[int] = Field(min_length=1, max_length=1000)


class CopilotIn(_Strict):
    question: str = Field(min_length=1, max_length=500)


class LoginIn(_Strict):
    email: str = Field(max_length=255)
    password: str = Field(max_length=1024)


class UserIn(_Strict):
    email: str = Field(max_length=255)
    password: str = Field(max_length=1024)
    name: str = Field(default="", max_length=255)
    role: Literal["admin", "member"] = "member"

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        email = check_email(v)
        if not email:
            raise ValueError("email is required")
        return email


class SlotTemplateIn(_Strict):
    weekday: int = Field(ge=0, le=6)
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
