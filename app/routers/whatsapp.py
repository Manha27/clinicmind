"""WhatsApp Cloud API (Meta) webhook: verification handshake + signed inbound messages."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_org
from app.jobs.handlers import notify
from app.jobs.queue import enqueue_extraction
from app.models import Lead, Message, Org, Thread, WebhookEvent

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])


async def raw_body(request: Request) -> bytes:
    return await request.body()


def signature_ok(body: bytes, header: str | None) -> bool:
    """Meta signs the raw body with the app secret: X-Hub-Signature-256: sha256=<hex>."""
    if not header or not settings.whatsapp_app_secret:
        return False
    expected = hmac.new(settings.whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.removeprefix("sha256=").encode())


@router.get("")
def verify(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta calls this once when you register the webhook; we must echo the challenge."""
    expected = settings.whatsapp_verify_token
    if mode == "subscribe" and expected and hmac.compare_digest((token or "").encode(), expected.encode()):
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "Verification failed")


def _find_or_create_lead(db: Session, org: Org, digits: str, name: str | None) -> tuple[Lead, bool]:
    lead = db.scalar(
        select(Lead).where(
            Lead.org_id == org.id, func.regexp_replace(Lead.phone, r"\D", "", "g") == digits
        )
    )
    if lead is not None:
        return lead, False
    lead = Lead(org_id=org.id, full_name=name, phone="+" + digits, source="whatsapp")
    db.add(lead)
    db.flush()
    return lead, True


def _ingest(db: Session, org: Org, msg: dict, names: dict) -> str:
    """Store one inbound WhatsApp message. Returns 'processed', 'duplicate' or 'failed'."""
    record = WebhookEvent(
        provider="whatsapp", external_id=msg["id"], payload=msg, signature_valid=True
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:  # Meta retries deliveries; the unique (provider, id) makes that safe
        db.rollback()
        return "duplicate"
    record_id = record.id

    try:
        sender = msg["from"]
        digits = re.sub(r"\D", "", sender)
        lead, created = _find_or_create_lead(db, org, digits, names.get(sender))
        thread = db.scalar(select(Thread).where(Thread.lead_id == lead.id, Thread.channel == "whatsapp"))
        if thread is None:
            thread = Thread(lead_id=lead.id, channel="whatsapp")
            db.add(thread)
            db.flush()
        kind = msg.get("type", "text")
        body = msg.get("text", {}).get("body") if kind == "text" else f"[{kind} message]"
        db.add(
            Message(
                thread_id=thread.id,
                lead_id=lead.id,
                direction="in",
                body=body or f"[{kind} message]",
                sent_at=datetime.fromtimestamp(int(msg.get("timestamp", 0)), tz=timezone.utc)
                if msg.get("timestamp")
                else datetime.now(timezone.utc),
                external_id=msg["id"],
            )
        )
        enqueue_extraction(db, lead.id)
        if created:
            notify(db, f"New WhatsApp enquiry from {lead.full_name or lead.phone}")
        event = db.get(WebhookEvent, record_id)
        event.status = "processed"
        event.attempts += 1
        db.commit()
        return "processed"
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        event = db.get(WebhookEvent, record_id)
        event.status = "failed"
        event.attempts += 1
        event.last_error = str(exc)[:500]
        db.commit()
        return "failed"


@router.post("")
def receive(
    body: bytes = Depends(raw_body),
    x_hub_signature_256: str | None = Header(None),
    db: Session = Depends(get_db),
    org: Org = Depends(get_org),
):
    if not settings.whatsapp_app_secret:
        raise HTTPException(503, "WHATSAPP_APP_SECRET is not set")
    if not signature_ok(body, x_hub_signature_256):
        raise HTTPException(401, "Invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")

    counts = {"processed": 0, "duplicate": 0, "failed": 0}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                counts[_ingest(db, org, msg, names)] += 1
    return {"status": "ok", **counts}  # always 200 once the signature is valid, so Meta stops retrying
