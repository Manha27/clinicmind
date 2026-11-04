from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_lead_or_404
from app.llm import LLMUnavailable, generate
from app.memory.context import build_context
from app.memory.retrieve import hybrid_search, search_memories
from app.memory.service import extract_pending
from app.models import Lead, Memory
from app.schemas import (
    AskRequest,
    AskResponse,
    ContextOut,
    HybridHit,
    MemoryHit,
    MemoryOut,
)

router = APIRouter(prefix="/leads/{lead_id}", tags=["memories"])

BUSY = "AI service is busy. Try again in a minute."

ANSWER_SYSTEM = """You help a clinic's front desk. Answer the question using ONLY the context provided.
If the context does not contain the answer, say you don't know. Be concise."""


@router.post("/extract", response_model=list[MemoryOut])
def extract(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    try:
        return extract_pending(db, lead)
    except LLMUnavailable:
        raise HTTPException(503, BUSY)
    except ValueError as exc:
        raise HTTPException(502, str(exc))


@router.get("/memories", response_model=list[MemoryOut])
def list_memories(
    status: Literal["active", "superseded", "all"] = "active",
    lead: Lead = Depends(get_lead_or_404),
    db: Session = Depends(get_db),
):
    stmt = select(Memory).where(Memory.scope == "lead", Memory.scope_id == lead.id)
    if status != "all":
        stmt = stmt.where(Memory.status == status)
    return db.scalars(stmt.order_by(Memory.created_at)).all()


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
        raise HTTPException(503, BUSY)
    return [
        MemoryHit(memory=MemoryOut.model_validate(m), similarity=round(s, 4))
        for m, s in hits
    ]


@router.get("/memories/retrieve", response_model=list[HybridHit])
def retrieve(
    q: str = Query(min_length=1),
    k: int = Query(5, ge=1, le=20),
    lead: Lead = Depends(get_lead_or_404),
    db: Session = Depends(get_db),
):
    try:
        hits = hybrid_search(db, lead, q, k)
    except LLMUnavailable:
        raise HTTPException(503, BUSY)
    return [
        HybridHit(
            memory=MemoryOut.model_validate(h.memory),
            score=round(h.score, 4),
            parts=h.parts,
        )
        for h in hits
    ]


@router.get("/context", response_model=ContextOut)
def context(
    q: str = Query(min_length=1),
    k: int = Query(8, ge=1, le=20),
    lead: Lead = Depends(get_lead_or_404),
    db: Session = Depends(get_db),
):
    """Show the prompt context that would be sent to the LLM (no LLM call)."""
    try:
        ctx = build_context(db, lead, q, k)
    except LLMUnavailable:
        raise HTTPException(503, BUSY)
    return ContextOut(text=ctx.text, manifest=ctx.manifest)


@router.post("/ask", response_model=AskResponse)
def ask(
    body: AskRequest,
    lead: Lead = Depends(get_lead_or_404),
    db: Session = Depends(get_db),
):
    try:
        ctx = build_context(db, lead, body.question)
        answer = generate(
            f"{ctx.text}\n\nQuestion: {body.question}",
            purpose="answer",
            system=ANSWER_SYSTEM,
            manifest=ctx.manifest,  # saved on the llm_calls row, for debugging and evals
        )
    except LLMUnavailable:
        raise HTTPException(503, BUSY)
    return AskResponse(answer=answer, context=ctx.text, manifest=ctx.manifest)
