from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import embed
from app.memory.extract import extract_memories
from app.memory.reconcile import decide, nearest_active
from app.models import Lead, Memory, MemoryEvent, Message

LABEL = {"in": "Patient", "out": "Clinic", "internal": "Staff note"}


def _log(db: Session, memory_id, op, reason, before=None, after=None) -> None:
    db.add(
        MemoryEvent(memory_id=memory_id, op=op, reason=reason, before=before, after=after)
    )


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

    now = datetime.now(timezone.utc)
    created: list[Memory] = []
    if candidates:
        vectors = embed([c.content for c in candidates])
        for c, vec in zip(candidates, vectors):
            op, target, reason = decide(c, nearest_active(db, lead.id, vec))

            if op == "NOOP":
                _log(db, target.id if target else None, "NOOP", reason,
                     after={"content": c.content})
                continue

            new = Memory(
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
            db.add(new)
            db.flush()  # gives new.id, and lets the next candidate see this one

            if op == "SUPERSEDE":
                before = {"id": str(target.id), "content": target.content}
                target.status = "superseded"
                target.valid_to = now
                target.superseded_by = new.id
                _log(db, new.id, "SUPERSEDE", reason, before=before,
                     after={"content": new.content})
            else:
                _log(db, new.id, "ADD", reason, after={"content": new.content})
            created.append(new)

    for m in pending:
        m.extracted_at = now  # so these messages are not processed twice
    db.commit()
    for mem in created:
        db.refresh(mem)
    return created