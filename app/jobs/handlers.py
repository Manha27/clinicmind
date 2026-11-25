"""What each kind of job actually does. Add a function + decorator to add a job type."""
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import settings
from app.integrations import gmail, http, slack
from app.jobs.queue import PermanentJobError, enqueue, enqueue_extraction  # noqa: F401
from app.memory.service import extract_pending
from app.models import IntegrationAccount, Lead

HANDLERS: dict[str, Callable[[Session, dict], dict]] = {}


def handler(kind: str):
    def register(fn):
        HANDLERS[kind] = fn
        return fn

    return register


def notify(db: Session, text: str) -> None:
    """Queue a Slack message (only if Slack is configured). Caller commits."""
    if settings.slack_webhook_url:
        enqueue(db, "slack_alert", {"text": text})


@handler("extract_lead")
def extract_lead(db: Session, payload: dict) -> dict:
    lead = db.get(Lead, uuid.UUID(payload["lead_id"]))
    if lead is None:
        raise PermanentJobError("lead no longer exists")
    created = extract_pending(db, lead)  # LLM errors propagate, so the job is retried
    for memory in created:
        if memory.kind == "commitment":
            notify(db, f"Follow-up promised for {lead.full_name or lead.email}: {memory.content}")
    db.commit()
    return {"memories_created": len(created)}


@handler("slack_alert")
def slack_alert(db: Session, payload: dict) -> dict:
    try:
        slack.send_message(payload["text"])
    except http.UpstreamError as exc:
        if exc.permanent:
            raise PermanentJobError(str(exc)) from exc
        raise
    return {"sent": True}


@handler("gmail_sync")
def gmail_sync(db: Session, payload: dict) -> dict:
    account = db.get(IntegrationAccount, uuid.UUID(payload["account_id"]))
    if account is None or account.status != "active":
        raise PermanentJobError("Google account missing or needs reconnecting")
    try:
        return gmail.sync_account(db, account)
    except http.UpstreamError as exc:
        if exc.permanent:
            raise PermanentJobError(str(exc)) from exc
        raise
