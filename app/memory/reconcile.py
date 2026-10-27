import logging
from typing import Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import generate
from app.memory.extract import CandidateMemory, _strip_fences
from app.models import Memory

logger = logging.getLogger("uvicorn.error")

DUPLICATE_SIM = 0.97  # at or above: same memory, no LLM call needed
RELATED_SIM = 0.70  # below: nothing related enough to conflict, so just ADD
MAX_NEIGHBOURS = 3

SYSTEM = """You maintain a patient's memory list for a clinic CRM.
Given EXISTING memories (numbered) and one NEW memory, decide what to do.

Return ONLY JSON: {"op": "ADD" | "SUPERSEDE" | "NOOP", "target": <number or null>, "reason": "<short>"}

- NOOP: the new memory says the same thing as an existing one. target = that number.
- SUPERSEDE: the same attribute now has a new value, so the new memory replaces an existing one
  (for example the budget changed, or the preferred time changed). target = the number it replaces.
- ADD: it is new information, or about a different attribute. Existing memories stay.

Memories about different attributes (budget vs treatment vs time slot) never conflict.
When unsure, choose ADD."""


class Decision(BaseModel):
    op: Literal["ADD", "SUPERSEDE", "NOOP"]
    target: int | None = None
    reason: str = ""


def nearest_active(
    db: Session, lead_id, vector: list[float], k: int = MAX_NEIGHBOURS
) -> list[tuple[Memory, float]]:
    distance = Memory.embedding.cosine_distance(vector)
    stmt = (
        select(Memory, distance.label("distance"))
        .where(
            Memory.scope == "lead",
            Memory.scope_id == lead_id,
            Memory.status == "active",
        )
        .order_by(distance)
        .limit(k)
    )
    return [(m, 1 - d) for m, d in db.execute(stmt).all()]


def decide(
    candidate: CandidateMemory, neighbours: list[tuple[Memory, float]]
) -> tuple[str, Memory | None, str]:
    """Return (op, memory it refers to, reason)."""
    if not neighbours:
        return "ADD", None, "no existing memories"

    best, best_sim = neighbours[0]
    if best_sim >= DUPLICATE_SIM:
        return "NOOP", best, f"duplicate (similarity {best_sim:.2f})"

    related = [m for m, sim in neighbours if sim >= RELATED_SIM]
    if not related:
        return "ADD", None, "nothing related"

    listing = "\n".join(f"{i}: [{m.kind}] {m.content}" for i, m in enumerate(related))
    prompt = (
        f"EXISTING:\n{listing}\n\nNEW:\n[{candidate.kind}] {candidate.content}\n\n"
        "Return the JSON."
    )
    raw = generate(
        prompt, purpose="reconcile", system=SYSTEM, json_mode=True, temperature=0.0
    )
    try:
        d = Decision.model_validate_json(_strip_fences(raw))
    except ValidationError:
        logger.warning("Reconcile output invalid: %r", raw[:300])
        return "ADD", None, "reconcile output invalid; added to be safe"

    if d.op == "ADD":
        return "ADD", None, d.reason
    if d.target is None or not 0 <= d.target < len(related):
        return "ADD", None, "reconcile gave no valid target; added to be safe"
    return d.op, related[d.target], d.reason