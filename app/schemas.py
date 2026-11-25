import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LeadStatus = Literal["new", "contacted", "consulted", "plan_sent", "won", "lost"]
Channel = Literal["email", "whatsapp", "note", "call"]
Direction = Literal["in", "out", "internal"]


class LeadCreate(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    source: str = "manual"
    profile: dict = Field(default_factory=dict)


class LeadUpdate(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    status: LeadStatus | None = None
    owner_id: uuid.UUID | None = None
    profile: dict | None = None


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str | None
    email: str | None
    phone: str | None
    source: str | None
    status: str
    profile: dict
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    channel: Channel = "note"
    direction: Direction = "internal"
    body: str = Field(min_length=1)
    sent_at: datetime | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    thread_id: uuid.UUID
    lead_id: uuid.UUID
    direction: str
    body: str
    sent_at: datetime


class PortalEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    treatment: str | None = None
    city: str | None = None
    message: str = Field(min_length=1)
    created_at: datetime | None = None


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    content: str
    importance: float
    confidence: float
    status: str
    created_at: datetime


class MemoryHit(BaseModel):
    memory: MemoryOut
    similarity: float


class HybridHit(BaseModel):
    memory: MemoryOut
    score: float
    parts: dict[str, float]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class ContextOut(BaseModel):
    text: str
    manifest: dict


class AskResponse(BaseModel):
    answer: str
    context: str
    manifest: dict


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    run_at: datetime
    last_error: str | None
    result: dict | None
    created_at: datetime
    finished_at: datetime | None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    title: str
    due_at: datetime | None
    status: str
    created_by: str
    created_at: datetime


class MemoryEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    memory_id: uuid.UUID | None
    op: str
    reason: str | None
    before: dict | None
    after: dict | None
    created_at: datetime


class IntegrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    account_email: str
    status: str
    scopes: list | None
    expires_at: datetime | None
    created_at: datetime


class AgentTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    lead_id: uuid.UUID | None = None
    allow_actions: bool = Field(
        False, description="You (the human) approve actions outside ClinicMind, like Slack or Google Docs"
    )
    history: list[AgentTurn] = Field(default_factory=list, max_length=20)


class AgentResponse(BaseModel):
    answer: str
    steps: list[dict]
    stopped: str
