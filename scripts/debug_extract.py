import uuid

from pydantic import ValidationError
from sqlalchemy import select

from app.db import SessionLocal
from app.llm import generate
from app.memory.extract import SYSTEM, Extraction, _strip_fences
from app.memory.service import LABEL
from app.models import Message

LEAD_ID = uuid.UUID("ff5e81e8-56eb-426a-90d4-b5ea4ed05e86")

with SessionLocal() as db:
    pending = db.scalars(
        select(Message)
        .where(Message.lead_id == LEAD_ID, Message.extracted_at.is_(None))
        .order_by(Message.sent_at)
    ).all()

transcript = "\n".join(
    f"[{m.sent_at:%Y-%m-%d %H:%M}] {LABEL[m.direction]}: {m.body}" for m in pending
)
print("--- TRANSCRIPT ---")
print(transcript)

raw = generate(
    f"Conversation:\n{transcript}\n\nReturn the JSON.",
    purpose="debug",
    system=SYSTEM,
    json_mode=True,
    temperature=0.0,
)
print("\n--- RAW MODEL OUTPUT ---")
print(raw)

print("\n--- VALIDATION ---")
try:
    result = Extraction.model_validate_json(_strip_fences(raw))
    print("OK:", len(result.memories), "memories")
except ValidationError as exc:
    print(exc)