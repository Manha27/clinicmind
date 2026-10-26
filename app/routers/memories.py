from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_lead_or_404
from app.memory.service import extract_pending
from app.models import Lead, Memory
from app.schemas import MemoryOut

router = APIRouter(prefix="/leads/{lead_id}", tags=["memories"])


@router.post("/extract", response_model=list[MemoryOut])
def extract(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    try:
        return extract_pending(db, lead)
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