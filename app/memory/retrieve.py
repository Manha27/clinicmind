from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import embed
from app.models import Lead, Memory


def search_memories(
    db: Session, lead: Lead, query: str, k: int = 5
) -> list[tuple[Memory, float]]:
    """Top-k active memories of this lead, most similar to the query first."""
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
    # cosine distance is 0 for identical meaning, so similarity = 1 - distance
    return [(memory, 1 - dist) for memory, dist in db.execute(stmt).all()]