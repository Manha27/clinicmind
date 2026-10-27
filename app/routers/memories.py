from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.llm import LLMUnavailable
from app.db import get_db
from app.deps import get_lead_or_404
from app.models import Lead, Memory
from app.memory.retrieve import search_memories
from app.memory.service import extract_pending
from app.schemas import MemoryHit, MemoryOut

router = APIRouter(prefix="/leads/{lead_id}", tags=["memories"])


@router.post("/extract", response_model=list[MemoryOut])
def extract(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    try:
        return extract_pending(db, lead)
    except LLMUnavailable:
        raise HTTPException(503, "AI service is busy. Try again in a minute.")
    except ValueError as exc:
        raise HTTPException(502, str(exc))

@router.get("/memories", response_model=list[MemoryOut])
def list_memories(
    lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)
):
    stmt = (
        select(Memory)
        .where(
            Memory.scope == "lead", Memory.scope_id == lead.id, Memory.status == "active"
        )
        .order_by(Memory.created_at)
    )
    return db.scalars(stmt).all()

@router.get("/memories/search", response_model=list[MemoryHit])
def search(
    q: str = Query(min_length=1),
    k: int = Query(5, ge=1, le=20),
    lead: Lead = Depends(get_lead_or_404),
    db: Session = Depends(get_db),
):
    try:
        hits = search_memories(db, lead, q, k)
    except LLMUnavailable:
        raise HTTPException(503, "AI service is busy. Try again in a minute.")
    return [
        MemoryHit(memory=MemoryOut.model_validate(m), similarity=round(s, 4))
        for m, s in hits
    ]