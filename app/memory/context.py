from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.retrieve import hybrid_search
from app.models import Lead, Message

CHARS_PER_TOKEN = 4  # rough estimate, good enough for budgeting
BUDGET = {"profile": 150, "memories": 600, "messages": 500}  # max tokens per section
KIND_TITLE = {
    "fact": "Facts",
    "preference": "Preferences",
    "commitment": "Commitments",
    "episode": "Events",
}
LABEL = {"in": "Patient", "out": "Clinic", "internal": "Staff note"}


def est_tokens(text: str) -> int:
    return max(1, -(-len(text) // CHARS_PER_TOKEN))


def _take(items, text_of, budget):
    """Keep items in order until the token budget is used up."""
    kept, used = [], 0
    for item in items:
        cost = est_tokens(text_of(item))
        if used + cost > budget:
            break
        kept.append(item)
        used += cost
    return kept, used


@dataclass
class Context:
    text: str
    manifest: dict


def build_context(db: Session, lead: Lead, question: str, k: int = 8) -> Context:
    # 1. profile: structured fields
    profile = [
        f"Name: {lead.full_name or 'unknown'}",
        f"Stage: {lead.status}",
        f"Source: {lead.source or 'unknown'}",
    ] + [f"{key}: {value}" for key, value in (lead.profile or {}).items()]
    profile_lines, profile_tokens = _take(profile, lambda s: s, BUDGET["profile"])

    # 2. memories: best-scoring first until the budget is full, then grouped by kind
    hits = hybrid_search(db, lead, question, k)
    kept_hits, memory_tokens = _take(hits, lambda h: h.memory.content, BUDGET["memories"])
    grouped: dict[str, list[str]] = {}
    for h in kept_hits:
        grouped.setdefault(h.memory.kind, []).append(h.memory.content)
    memory_lines = []
    for kind, items in grouped.items():
        memory_lines.append(f"{KIND_TITLE.get(kind, kind)}:")
        memory_lines += [f"- {c}" for c in items]

    # 3. recent messages: newest first until the budget is full, then oldest first
    recent = db.scalars(
        select(Message)
        .where(Message.lead_id == lead.id)
        .order_by(Message.sent_at.desc())
        .limit(10)
    ).all()

    def line_of(m: Message) -> str:
        return f"[{m.sent_at:%Y-%m-%d %H:%M}] {LABEL[m.direction]}: {m.body}"

    kept_messages, message_tokens = _take(recent, line_of, BUDGET["messages"])
    kept_messages.reverse()

    sections = ["## Patient profile", *profile_lines, "", "## What we remember"]
    sections += memory_lines or ["(nothing remembered yet)"]
    sections += ["", "## Recent messages"]
    sections += [line_of(m) for m in kept_messages] or ["(no messages)"]

    manifest = {
        "question": question,
        "memory_ids": [str(h.memory.id) for h in kept_hits],
        "memory_scores": {str(h.memory.id): round(h.score, 3) for h in kept_hits},
        "message_ids": [str(m.id) for m in kept_messages],
        "tokens": {
            "profile": profile_tokens,
            "memories": memory_tokens,
            "messages": message_tokens,
            "total": profile_tokens + memory_tokens + message_tokens,
        },
    }
    return Context(text="\n".join(sections), manifest=manifest)
