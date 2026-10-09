from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), default="", nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(32), default="member", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


# --- Product models are appended below by each repo's builder ---


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), default="", nullable=False)
    email = Column(String(255), default="", nullable=False)
    phone = Column(String(64), default="", nullable=False)
    company = Column(String(255), default="", nullable=False)
    source = Column(String(64), default="manual", nullable=False)
    stage = Column(String(32), default="New", nullable=False, index=True)
    intent = Column(String(32), nullable=True, index=True)
    intent_confidence = Column(Float, nullable=True)
    intent_reasons_json = Column(Text, default="[]", nullable=False)
    slots_json = Column(Text, default="{}", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    role = Column(String(16), nullable=False)  # lead | agent | system
    text = Column(Text, nullable=False)
    channel = Column(String(32), default="simulator", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    kind = Column(String(64), nullable=False)
    detail_json = Column(Text, default="{}", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    title = Column(String(255), default="", nullable=False)
    starts_at = Column(DateTime, nullable=False, index=True)
    ends_at = Column(DateTime, nullable=False)
    attendee_email = Column(String(255), default="", nullable=False)
    ics_uid = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class SlotTemplate(Base):
    __tablename__ = "slot_templates"

    id = Column(Integer, primary_key=True)
    weekday = Column(Integer, nullable=False)  # 0=Monday .. 6=Sunday
    start_time = Column(String(5), default="09:00", nullable=False)
    end_time = Column(String(5), default="17:00", nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    template_text = Column(Text, nullable=False)
    followup_hours = Column(Integer, default=24, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    last_agent_msg_at = Column(DateTime, nullable=True)
    status = Column(String(32), default="active", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class FeedToken(Base):
    __tablename__ = "feed_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
