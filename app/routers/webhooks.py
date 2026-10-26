import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_org
from app.models import Lead, Message, Org, Thread, WebhookEvent
from app.schemas import PortalEvent

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_AGE_SECONDS = 300  # reject requests older than 5 minutes (replay protection)


async def raw_body(request: Request) -> bytes:
    # The signature covers the exact bytes that were sent, so we read
    # the raw body before anything parses it as JSON.
    return await request.body()


def signature_is_valid(body: bytes, timestamp: str, signature: str) -> bool:
    signed = timestamp.encode() + b"." + body
    expected = hmac.new(
        settings.webhook_secret.encode(), signed, hashlib.sha256
    ).hexdigest()
    received = signature.removeprefix("sha256=")
    # compare_digest takes constant time, so attackers can't guess the
    # signature character by character from response timing
    return hmac.compare_digest(expected.encode(), received.encode())


def ingest_portal_event(db: Session, org: Org, event: PortalEvent) -> Lead:
    """Turn a portal event into a lead + message. Week 4 moves this into a background job."""
    email = event.email.strip().lower() if event.email else None

    lead = None
    if email:
        lead = db.scalar(select(Lead).where(Lead.org_id == org.id, Lead.email == email))
    if lead is None:
        profile = {"treatment_interest": event.treatment, "city": event.city}
        lead = Lead(
            org_id=org.id,
            full_name=event.name,
            email=email,
            phone=event.phone,
            source="portal_webhook",
            profile={k: v for k, v in profile.items() if v},
        )
        db.add(lead)
        db.flush()

    thread = db.scalar(
        select(Thread).where(Thread.lead_id == lead.id, Thread.channel == "portal")
    )
    if thread is None:
        thread = Thread(lead_id=lead.id, channel="portal")
        db.add(thread)
        db.flush()

    db.add(
        Message(
            thread_id=thread.id,
            lead_id=lead.id,
            direction="in",
            body=event.message,
            sent_at=event.created_at or datetime.now(timezone.utc),
            external_id=event.event_id,
        )
    )
    return lead


@router.post("/portal")
def portal_webhook(
    body: bytes = Depends(raw_body),
    x_timestamp: str | None = Header(None),
    x_signature: str | None = Header(None),
    db: Session = Depends(get_db),
    org: Org = Depends(get_org),
):
    # 1. Authenticate: reject before touching the database
    if not x_timestamp or not x_signature:
        raise HTTPException(401, "Missing signature headers")
    try:
        age = abs(time.time() - int(x_timestamp))
    except ValueError:
        raise HTTPException(401, "Bad timestamp")
    if age > MAX_AGE_SECONDS:
        raise HTTPException(401, "Timestamp too old")
    if not signature_is_valid(body, x_timestamp, x_signature):
        raise HTTPException(401, "Invalid signature")

    # 2. Validate the payload shape
    try:
        event = PortalEvent.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "Invalid payload")

    # 3. Store the raw event first. The unique (provider, external_id)
    #    constraint makes a repeated event fail here: that's idempotency.
    record = WebhookEvent(
        provider="portal",
        external_id=event.event_id,
        payload=json.loads(body),
        signature_valid=True,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}  # 200, so the sender stops retrying
    record_id = record.id

    # 4. Process. Lead, message and event status commit together or not at all.
    try:
        lead = ingest_portal_event(db, org, event)
        lead_id = lead.id
        record = db.get(WebhookEvent, record_id)
        record.status = "processed"
        record.attempts += 1
        db.commit()
        return {"status": "processed", "lead_id": str(lead_id)}
    except Exception as exc:
        db.rollback()
        record = db.get(WebhookEvent, record_id)
        record.status = "failed"
        record.attempts += 1
        record.last_error = str(exc)[:500]
        db.commit()
        # the event is saved, so we can retry it later (Week 4)
        return {"status": "failed"}