"""The tool layer: ONE definition of what an AI agent can do with ClinicMind.

The built-in agent (app/agent.py) and the MCP server (mcp_server/server.py) both call
`run_tool`, so a tool behaves identically wherever it is used and has one place to fix bugs.

Safety rules:
  * every tool validates its input with Pydantic before touching the database;
  * tools that send anything OUTSIDE the system (Slack, Google Docs) do nothing unless the
    HUMAN has approved actions (ToolContext.allow_external). The model cannot approve itself.
"""
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations import gdocs, google_oauth, http
from app.jobs.queue import enqueue, enqueue_extraction
from app.llm import generate
from app.memory.context import build_context
from app.memory.retrieve import hybrid_search
from app.models import Lead, Memory, Message, Org, Task, Thread


class ToolError(Exception):
    """A problem the model should be told about (bad id, missing setup...)."""


@dataclass
class ToolContext:
    db: Session
    org: Org
    allow_external: bool = False  # set by a human, never by the model


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[ToolContext, BaseModel], dict]
    mutating: bool = False
    external: bool = False  # sends data outside ClinicMind


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, model: type[BaseModel], *, mutating=False, external=False):
    def register(fn):
        TOOLS[name] = Tool(name, description, model, fn, mutating, external)
        return fn

    return register


def _lead(ctx: ToolContext, lead_id: str) -> Lead:
    try:
        lead = ctx.db.scalar(
            select(Lead).where(Lead.id == uuid.UUID(lead_id), Lead.org_id == ctx.org.id)
        )
    except ValueError:
        raise ToolError(f"{lead_id!r} is not a valid lead id (use the id from search_leads)")
    if lead is None:
        raise ToolError("Lead not found")
    return lead


def _needs_approval(ctx: ToolContext, confirm: bool, what: str) -> dict | None:
    if confirm and ctx.allow_external:
        return None
    return {
        "status": "needs_confirmation",
        "message": f"I have not done this yet. {what} Ask the user to approve, then call again with confirm=true.",
    }


# ---------------- read-only tools ----------------

class SearchLeadsInput(BaseModel):
    query: str = Field(description="Part of a name, email or phone number")
    status: Literal["new", "contacted", "consulted", "plan_sent", "won", "lost"] | None = Field(
        None, description="Only leads at this stage"
    )
    limit: int = Field(10, ge=1, le=25)


@tool("search_leads", "Find leads by name, email or phone number. Returns ids to use in other tools.", SearchLeadsInput)
def search_leads(ctx: ToolContext, a: SearchLeadsInput) -> dict:
    like = f"%{a.query}%"
    stmt = select(Lead).where(
        Lead.org_id == ctx.org.id,
        or_(Lead.full_name.ilike(like), Lead.email.ilike(like), Lead.phone.ilike(like)),
    )
    if a.status:
        stmt = stmt.where(Lead.status == a.status)
    leads = ctx.db.scalars(stmt.order_by(Lead.created_at.desc()).limit(a.limit)).all()
    return {"leads": [{"id": str(l.id), "name": l.full_name, "email": l.email, "phone": l.phone, "status": l.status} for l in leads]}


class LeadIdInput(BaseModel):
    lead_id: str = Field(description="Lead id from search_leads")


@tool("get_lead", "Get one lead's profile, stage and activity counts.", LeadIdInput)
def get_lead(ctx: ToolContext, a: LeadIdInput) -> dict:
    lead = _lead(ctx, a.lead_id)
    db = ctx.db
    count = lambda model, *where: db.scalar(select(func.count()).select_from(model).where(*where))  # noqa: E731
    return {
        "id": str(lead.id), "name": lead.full_name, "email": lead.email, "phone": lead.phone,
        "status": lead.status, "source": lead.source, "profile": lead.profile,
        "messages": count(Message, Message.lead_id == lead.id),
        "active_memories": count(Memory, Memory.scope == "lead", Memory.scope_id == lead.id, Memory.status == "active"),
        "open_tasks": count(Task, Task.lead_id == lead.id, Task.status == "open"),
    }


class LeadContextInput(BaseModel):
    lead_id: str
    question: str = Field(description="What you want to know; used to pick the most relevant memories")


@tool("get_lead_context", "Get a ready-to-read summary of a lead (profile, relevant memories, recent messages) for a question.", LeadContextInput)
def get_lead_context(ctx: ToolContext, a: LeadContextInput) -> dict:
    ctxt = build_context(ctx.db, _lead(ctx, a.lead_id), a.question)
    return {"context": ctxt.text, "tokens": ctxt.manifest["tokens"]["total"]}


class SearchMemoriesInput(BaseModel):
    lead_id: str
    query: str
    k: int = Field(5, ge=1, le=15)


@tool("search_memories", "Find the remembered facts, preferences and promises about a lead that best match a query.", SearchMemoriesInput)
def search_memories(ctx: ToolContext, a: SearchMemoriesInput) -> dict:
    hits = hybrid_search(ctx.db, _lead(ctx, a.lead_id), a.query, a.k)
    return {"memories": [{"kind": h.memory.kind, "content": h.memory.content, "score": round(h.score, 3), "remembered_on": h.memory.created_at.date().isoformat()} for h in hits]}


class ListTasksInput(BaseModel):
    lead_id: str | None = Field(None, description="Limit to one lead (optional)")
    status: Literal["open", "done", "all"] = "open"


@tool("list_tasks", "List follow-up tasks, for one lead or for everyone.", ListTasksInput)
def list_tasks(ctx: ToolContext, a: ListTasksInput) -> dict:
    stmt = select(Task).where(Task.org_id == ctx.org.id)
    if a.lead_id:
        stmt = stmt.where(Task.lead_id == _lead(ctx, a.lead_id).id)
    if a.status != "all":
        stmt = stmt.where(Task.status == a.status)
    tasks = ctx.db.scalars(stmt.order_by(Task.due_at.asc().nulls_last(), Task.created_at).limit(50)).all()
    return {"tasks": [{"id": str(t.id), "lead_id": str(t.lead_id), "title": t.title, "due_at": t.due_at.isoformat() if t.due_at else None, "status": t.status} for t in tasks]}


@tool("suggest_next_step", "Ask the AI for the single best next action to move a lead forward, based on what we remember.", LeadIdInput)
def suggest_next_step(ctx: ToolContext, a: LeadIdInput) -> dict:
    lead = _lead(ctx, a.lead_id)
    ctxt = build_context(ctx.db, lead, "What is the best next step to move this patient forward?")
    text = generate(
        f"{ctxt.text}\n\nSuggest the single best next action for the front desk, in two sentences.",
        purpose="suggest",
        system="You advise a clinic front desk. Use only the context. Be specific and brief.",
        manifest=ctxt.manifest,
    )
    return {"suggestion": text}


# ---------------- tools that change ClinicMind data ----------------

class AddNoteInput(BaseModel):
    lead_id: str
    text: str = Field(min_length=1, max_length=2000)
    idempotency_key: str | None = Field(None, description="Same key twice = the note is only saved once")


@tool("add_note", "Save a staff note on a lead. The note is also turned into memories in the background.", AddNoteInput, mutating=True)
def add_note(ctx: ToolContext, a: AddNoteInput) -> dict:
    lead, db = _lead(ctx, a.lead_id), ctx.db
    thread = db.scalar(select(Thread).where(Thread.lead_id == lead.id, Thread.channel == "note"))
    if thread is None:
        thread = Thread(lead_id=lead.id, channel="note")
        db.add(thread)
        db.flush()
    if a.idempotency_key:
        existing = db.scalar(select(Message).where(Message.thread_id == thread.id, Message.external_id == a.idempotency_key))
        if existing:
            return {"status": "duplicate", "message_id": str(existing.id)}
    message = Message(
        thread_id=thread.id, lead_id=lead.id, direction="internal", body=a.text,
        sent_at=datetime.now(timezone.utc), external_id=a.idempotency_key,
    )
    db.add(message)
    enqueue_extraction(db, lead.id)
    db.commit()
    return {"status": "saved", "message_id": str(message.id)}


class CreateTaskInput(BaseModel):
    lead_id: str
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime | None = Field(None, description="ISO 8601 date-time, optional")


@tool("create_task", "Create a follow-up task for a lead. Creating the same open task twice returns the existing one.", CreateTaskInput, mutating=True)
def create_task(ctx: ToolContext, a: CreateTaskInput) -> dict:
    lead, db = _lead(ctx, a.lead_id), ctx.db
    existing = db.scalar(select(Task).where(Task.lead_id == lead.id, Task.status == "open", func.lower(Task.title) == a.title.lower()))
    if existing:
        return {"status": "already_exists", "task_id": str(existing.id)}
    task = Task(org_id=ctx.org.id, lead_id=lead.id, title=a.title, due_at=a.due_at, created_by="agent")
    db.add(task)
    db.commit()
    return {"status": "created", "task_id": str(task.id)}


# ---------------- tools that act OUTSIDE ClinicMind (need human approval) ----------------

class SendSlackInput(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    confirm: bool = Field(False, description="Only true after the user explicitly approved sending")


@tool("send_slack_alert", "Post a message to the clinic's Slack channel. Requires the user's approval.", SendSlackInput, mutating=True, external=True)
def send_slack_alert(ctx: ToolContext, a: SendSlackInput) -> dict:
    pending = _needs_approval(ctx, a.confirm, f"This would post to Slack: {a.text!r}.")
    if pending:
        return pending
    if not settings.slack_webhook_url:
        raise ToolError("Slack is not configured (SLACK_WEBHOOK_URL)")
    job = enqueue(ctx.db, "slack_alert", {"text": a.text}, commit=True)
    return {"status": "queued", "job_id": str(job.id)}


class PlanDocInput(BaseModel):
    lead_id: str
    confirm: bool = Field(False, description="Only true after the user explicitly approved creating the document")


@tool("create_treatment_plan_doc", "Create a Google Doc summarising a lead's treatment plan. Requires the user's approval.", PlanDocInput, mutating=True, external=True)
def create_treatment_plan_doc(ctx: ToolContext, a: PlanDocInput) -> dict:
    lead = _lead(ctx, a.lead_id)
    pending = _needs_approval(ctx, a.confirm, f"This would create a Google Doc for {lead.full_name or lead.email}.")
    if pending:
        return pending
    try:
        return {"status": "created", "url": gdocs.create_plan_doc(ctx.db, ctx.org, lead)}
    except google_oauth.NotConnected as exc:
        raise ToolError(str(exc))
    except http.UpstreamError as exc:
        raise ToolError(f"Google Docs failed: {exc}")


# ---------------- running tools ----------------

def run_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    """Validate the arguments and run one tool. Problems come back as {"error": ...}
    so the model can read them and correct itself. (LLM outages still raise.)"""
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"Unknown tool {name!r}. Available: {', '.join(TOOLS)}"}
    try:
        parsed = spec.input_model.model_validate(args or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return {"error": f"Invalid arguments: {problems}"}
    try:
        result = spec.fn(ctx, parsed)
    except ToolError as exc:
        ctx.db.rollback()
        return {"error": str(exc)}
    return json.loads(json.dumps(result, default=str))  # guarantee JSON-safe output


def _simplify(schema):
    """Pydantic's JSON schema -> the simpler form model APIs accept (no titles, no null unions).
    Careful: a FIELD may itself be called "title", so only schema keywords are stripped,
    never the names inside "properties"."""
    if isinstance(schema, dict):
        if "anyOf" in schema:
            options = [o for o in schema["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                rest = {k: v for k, v in schema.items() if k != "anyOf"}
                return _simplify({**options[0], **rest})
        out = {}
        for key, value in schema.items():
            if key in ("title", "default"):
                continue
            if key == "properties":
                out[key] = {name: _simplify(sub) for name, sub in value.items()}
            else:
                out[key] = _simplify(value)
        return out
    if isinstance(schema, list):
        return [_simplify(v) for v in schema]
    return schema


def json_schema(spec: Tool) -> dict:
    return _simplify(spec.input_model.model_json_schema())
