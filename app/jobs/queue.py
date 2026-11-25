"""A small Postgres-backed job queue.

Why Postgres and not Redis: one less service to run, jobs are created in the same
transaction as the data that caused them, and `FOR UPDATE SKIP LOCKED` lets several
workers pull jobs without ever running the same job twice.
"""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help (job goes straight to 'dead')."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    db: Session,
    kind: str,
    payload: dict | None = None,
    *,
    dedupe_key: str | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 5,
    commit: bool = False,
) -> Job:
    """Add a job. With a dedupe_key, a second *queued* job with the same key is not added;
    the existing one is returned instead."""
    job = Job(
        kind=kind,
        payload=payload or {},
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
        run_at=run_at or _now(),
    )
    if dedupe_key is None:
        db.add(job)
        db.flush()
    else:
        try:
            with db.begin_nested():  # savepoint: a duplicate must not poison the caller's transaction
                db.add(job)
                db.flush()
        except IntegrityError:
            existing = db.scalar(
                select(Job).where(Job.dedupe_key == dedupe_key, Job.status == "queued")
            )
            if existing is None:
                raise
            job = existing
    if commit:
        db.commit()
    return job


def claim_next(db: Session) -> Job | None:
    """Take the oldest due job and mark it running. Safe with many workers."""
    job = db.scalar(
        select(Job)
        .where(Job.status == "queued", Job.run_at <= func.now())
        .order_by(Job.run_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.status = "running"
    job.locked_at = _now()
    job.attempts += 1
    db.commit()
    return job


def complete(db: Session, job: Job, result: dict | None = None) -> None:
    job.status = "done"
    job.result = result or {}
    job.locked_at = None
    job.last_error = None
    job.finished_at = _now()
    db.commit()


def backoff_seconds(attempts: int) -> float:
    return min(300, 5 * 2 ** max(0, attempts - 1)) + random.random()


def fail(db: Session, job: Job, exc: Exception, *, permanent: bool = False) -> None:
    """Record the error. Retry later with backoff, or mark dead when attempts run out."""
    job.last_error = f"{type(exc).__name__}: {exc}"[:500]
    job.locked_at = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = "dead"
        job.finished_at = _now()
    else:
        job.status = "queued"
        job.run_at = _now() + timedelta(seconds=backoff_seconds(job.attempts))
    db.commit()


def requeue_stale(db: Session) -> int:
    """A worker that crashed leaves its job 'running' forever; put those back in the queue."""
    cutoff = _now() - timedelta(seconds=settings.job_lock_timeout_s)
    stale = (Job.status == "running") & (Job.locked_at < cutoff)
    dead = db.execute(
        update(Job)
        .where(stale, Job.attempts >= Job.max_attempts)
        .values(status="dead", last_error="worker crashed too many times", finished_at=_now())
    ).rowcount
    revived = db.execute(
        update(Job).where(stale).values(status="queued", locked_at=None, run_at=_now())
    ).rowcount
    db.commit()
    return (dead or 0) + (revived or 0)


def retry(db: Session, job: Job) -> Job:
    """Manually put a dead job back in the queue (the dead-letter 'retry' button)."""
    job.status = "queued"
    job.attempts = 0
    job.run_at = _now()
    job.last_error = None
    job.finished_at = None
    db.commit()
    return job


def enqueue_extraction(db: Session, lead_id) -> Job:
    """Queue memory extraction for a lead. One queued job per lead is enough."""
    return enqueue(db, "extract_lead", {"lead_id": str(lead_id)}, dedupe_key=f"extract:{lead_id}")
