import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

from app.llm import embed
from app.models import Lead, Memory

# final score = weighted sum of four signals, each scaled to 0..1
W_VECTOR, W_KEYWORD, W_RECENCY, W_IMPORTANCE = 0.55, 0.20, 0.15, 0.10
HALF_LIFE_DAYS = 30  # a memory loses half its recency score every 30 days
CANDIDATES = 20  # how many vector matches get re-ranked


def search_memories(
    db: Session, lead: Lead, query: str, k: int = 5
) -> list[tuple[Memory, float]]:
    """Top-k active memories of this lead by vector similarity only."""
    query_vector = embed([query], task="RETRIEVAL_QUERY")[0]
    distance = Memory.embedding.cosine_distance(query_vector)
    stmt = (
        select(Memory, distance.label("distance"))
        .where(
            Memory.scope == "lead",
            Memory.scope_id == lead.id,
            Memory.status == "active",
        )
        .order_by(distance)
        .limit(k)
    )
    return [(memory, 1 - dist) for memory, dist in db.execute(stmt).all()]


@dataclass
class Hit:
    memory: Memory
    score: float
    parts: dict


def _or_query(text: str) -> str:
    # "who makes the decision" -> "who or makes or the or decision"
    # so one matching word is enough (stop words are dropped by Postgres)
    return " or ".join(re.findall(r"\w+", text))


def hybrid_search(db: Session, lead: Lead, query: str, k: int = 8) -> list[Hit]:
    query_vector = embed([query], task="RETRIEVAL_QUERY")[0]
    distance = Memory.embedding.cosine_distance(query_vector)
    english = literal_column("'english'::regconfig")
    # flag 32 squashes the keyword rank into 0..1
    keyword = func.ts_rank(
        func.to_tsvector(english, Memory.content),
        func.websearch_to_tsquery(english, _or_query(query)),
        literal_column("32"),
    )
    stmt = (
        select(Memory, distance.label("distance"), keyword.label("keyword"))
        .where(
            Memory.scope == "lead",
            Memory.scope_id == lead.id,
            Memory.status == "active",
        )
        .order_by(distance)
        .limit(CANDIDATES)
    )

    now = datetime.now(timezone.utc)
    hits = []
    for memory, dist, kw in db.execute(stmt).all():
        similarity = 1 - dist
        # Raw cosine scores for short sentences mostly land between ~0.4 and ~0.8
        # (you saw 0.60 for irrelevant and 0.73 for a real match), so stretch that band.
        vector = min(1.0, max(0.0, (similarity - 0.4) / 0.4))
        age_days = max(0.0, (now - memory.created_at).total_seconds() / 86400)
        recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
        keyword_score = float(kw or 0)
        score = (
            W_VECTOR * vector
            + W_KEYWORD * keyword_score
            + W_RECENCY * recency
            + W_IMPORTANCE * memory.importance
        )
        parts = {
            "vector": round(vector, 3),
            "keyword": round(keyword_score, 3),
            "recency": round(recency, 3),
            "importance": round(memory.importance, 3),
        }
        hits.append(Hit(memory=memory, score=score, parts=parts))

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]
