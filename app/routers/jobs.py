import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_lead_or_404
from app.jobs import queue
from app.models import Job, Lead
from app.schemas import JobOut

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    status: str | None = Query(None, pattern="^(queued|running|done|dead)$"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Job.status == status)
    return db.scalars(stmt).all()


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    """Dead-letter queue: put a dead job back in the queue."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status != "dead":
        raise HTTPException(409, "Only dead jobs can be retried")
    return queue.retry(db, job)


@router.post("/leads/{lead_id}/extract/async", response_model=JobOut, status_code=202)
def extract_async(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    """Same as POST /extract, but returns at once. The worker does the slow LLM calls."""
    job = queue.enqueue_extraction(db, lead.id)
    db.commit()
    db.refresh(job)
    return job
