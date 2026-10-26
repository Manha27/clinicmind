from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_lead_or_404
from app.models import Lead, Message, Thread
from app.schemas import MessageCreate, MessageOut

router = APIRouter(prefix="/leads/{lead_id}/messages", tags=["messages"])


@router.post("", response_model=MessageOut, status_code=201)
def add_message(
    data: MessageCreate,
    lead: Lead = Depends(get_lead_or_404),
    db: Session = Depends(get_db),
):
    # one thread per lead per channel; create it if missing
    thread = db.scalar(
        select(Thread).where(Thread.lead_id == lead.id, Thread.channel == data.channel)
    )
    if thread is None:
        thread = Thread(lead_id=lead.id, channel=data.channel)
        db.add(thread)
        db.flush()

    message = Message(
        thread_id=thread.id,
        lead_id=lead.id,
        direction=data.direction,
        body=data.body,
        sent_at=data.sent_at or datetime.now(timezone.utc),
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


@router.get("", response_model=list[MessageOut])
def list_messages(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    stmt = select(Message).where(Message.lead_id == lead.id).order_by(Message.sent_at)
    return db.scalars(stmt).all()