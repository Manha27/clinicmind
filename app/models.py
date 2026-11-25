import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, LargeBinary, Numeric, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
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


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_scope", "scope", "scope_id", "status"),
        Index(
            "ix_memories_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    scope: Mapped[str]  # lead | deal | rep
    scope_id: Mapped[uuid.UUID]  # the lead / deal / user this memory belongs to
    kind: Mapped[str]  # fact | preference | episode | commitment | summary
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embed_dim))
    importance: Mapped[float] = mapped_column(server_default="0.5")
    confidence: Mapped[float] = mapped_column(server_default="0.8")
    status: Mapped[str] = mapped_column(server_default="active")
    valid_from: Mapped[datetime] = mapped_column(**NOW)
    valid_to: Mapped[datetime | None]
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memories.id"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(**NOW)


class MemoryEvent(Base):
    """Audit trail: every memory write decision, with the reason."""

    __tablename__ = "memory_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    memory_id: Mapped[uuid.UUID | None]
    op: Mapped[str]  # ADD, SUPERSEDE, NOOP
    reason: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None]
    after: Mapped[dict | None]
    created_at: Mapped[datetime] = mapped_column(**NOW)


class Job(Base):
    """Background job queue (Postgres-backed; workers claim rows with SKIP LOCKED)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_run_at", "status", "run_at"),
        # at most one *queued* job per dedupe_key (a running one doesn't block a new one)
        Index(
            "uq_jobs_queued_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    kind: Mapped[str]  # extract_lead, slack_alert, gmail_sync
    payload: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(server_default="queued")  # queued|running|done|dead
    attempts: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="5")
    run_at: Mapped[datetime] = mapped_column(**NOW)
    locked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None]
    dedupe_key: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(**NOW)
    finished_at: Mapped[datetime | None]


class IntegrationAccount(Base):
    """A connected third-party account. Tokens are stored encrypted."""

    __tablename__ = "integration_accounts"
    __table_args__ = (UniqueConstraint("org_id", "provider", "account_email"),)

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    provider: Mapped[str]  # google
    account_email: Mapped[str]
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None]
    scopes: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(server_default="active")  # active | needs_reauth
    created_at: Mapped[datetime] = mapped_column(**NOW)


class SyncCursor(Base):
    """Where the last sync stopped (for Gmail: the historyId)."""

    __tablename__ = "sync_cursors"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    resource: Mapped[str] = mapped_column(primary_key=True)
    cursor: Mapped[str | None]
    last_synced_at: Mapped[datetime | None]


class OAuthState(Base):
    """One-time state + PKCE verifier for an in-progress OAuth login."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(primary_key=True)
    code_verifier: Mapped[str]
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    created_at: Mapped[datetime] = mapped_column(**NOW)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(server_default="open")  # open | done
    created_by: Mapped[str] = mapped_column(server_default="user")  # user | agent
    created_at: Mapped[datetime] = mapped_column(**NOW)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    strategy: Mapped[str]  # full_history | summary_only | memory
    config: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    summary: Mapped[dict | None]
    started_at: Mapped[datetime] = mapped_column(**NOW)
    finished_at: Mapped[datetime | None]


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True
    )
    case_key: Mapped[str]
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    correct: Mapped[bool]
    stale: Mapped[bool] = mapped_column(server_default=text("false"))
    tags: Mapped[list | None] = mapped_column(JSONB)
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    error: Mapped[str | None] = mapped_column(Text)
