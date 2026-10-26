from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import embed
from app.memory.extract import extract_memories
from app.models import Lead, Memory, Message

LABEL = {"in": "Patient", "out": "Clinic", "internal": "Staff note"}


def extract_pending(db: Session, lead: Lead) -> list[Memory]:
    pending = db.scalars(
        select(Message)
        .where(Message.lead_id == lead.id, Message.extracted_at.is_(None))
        .order_by(Message.sent_at)
    ).all()
    if not pending:
        return []

    transcript = "\n".join(
        f"[{m.sent_at:%Y-%m-%d %H:%M}] {LABEL[m.direction]}: {m.body}" for m in pending
    )
    candidates = extract_memories(transcript)  # raises ValueError on bad JSON

    memories: list[Memory] = []
    if candidates:
        vectors = embed([c.content for c in candidates])
        for c, vec in zip(candidates, vectors):
            memories.append(
                Memory(
                    org_id=lead.org_id,
                    scope="lead",
                    scope_id=lead.id,
                    kind=c.kind,
                    content=c.content,
                    embedding=vec,
                    importance=c.importance,
                    confidence=c.confidence,
                    source_message_id=pending[-1].id,  # approximate: last message in the batch
                )
            )
        db.add_all(memories)

    now = datetime.now(timezone.utc)
    for m in pending:
        m.extracted_at = now  # so these messages are not processed twice
    db.commit()
    for mem in memories:
        db.refresh(mem)
    return memories