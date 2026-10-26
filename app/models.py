import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Numeric, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

UUID_PK = dict(primary_key=True, server_default=text("gen_random_uuid()"))
NOW = dict(server_default=text("now()"))


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(**NOW)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    email: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str | None]
    role: Mapped[str] = mapped_column(server_default="rep")  # admin | rep


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("org_id", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), index=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    full_name: Mapped[str | None]
    email: Mapped[str | None]
    phone: Mapped[str | None]
    source: Mapped[str | None]  # portal_webhook, gmail, whatsapp, manual
    status: Mapped[str] = mapped_column(server_default="new")
    profile: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(**NOW)
    updated_at: Mapped[datetime] = mapped_column(**NOW)


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (UniqueConstraint("channel", "external_thread_id"),)

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str]  # email, whatsapp, note, call
    external_thread_id: Mapped[str | None]


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("thread_id", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE")
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str]  # in, out, internal
    body: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime]
    external_id: Mapped[str | None]
    extracted_at: Mapped[datetime | None]  # null = memory extraction pending


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)  # idempotency

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    provider: Mapped[str]
    external_id: Mapped[str]
    payload: Mapped[dict]
    signature_valid: Mapped[bool | None]
    status: Mapped[str] = mapped_column(server_default="received")
    attempts: Mapped[int] = mapped_column(server_default="0")
    last_error: Mapped[str | None]
    received_at: Mapped[datetime] = mapped_column(**NOW)

class LLMCall(Base):
    """One row per LLM or embedding call. Evals read this table."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    purpose: Mapped[str]  # extract, reconcile, answer, summarise, judge, embed
    model: Mapped[str]
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    latency_ms: Mapped[int]
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    ok: Mapped[bool] = mapped_column(server_default=text("true"))
    error: Mapped[str | None] = mapped_column(Text)
    context_manifest: Mapped[dict | None]  # which memories/messages went in
    created_at: Mapped[datetime] = mapped_column(**NOW)