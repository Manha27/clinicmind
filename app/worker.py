"""Background worker. Run it in a second terminal:   python -m app.worker"""
import logging
import time

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.jobs import handlers
from app.jobs.queue import PermanentJobError, claim_next, complete, fail, requeue_stale

log = logging.getLogger("worker")


def run_once(session_factory=SessionLocal) -> bool:
    """Run one due job. Returns False when the queue is empty."""
    with session_factory() as db:
        job = claim_next(db)
        if job is None:
            return False
        kind, job_id = job.kind, job.id
        try:
            fn = handlers.HANDLERS.get(kind)
            if fn is None:
                raise PermanentJobError(f"no handler for job kind {kind!r}")
            result = fn(db, job.payload)
            complete(db, job, result)
            log.info("job %s (%s) done", job_id, kind)
        except PermanentJobError as exc:
            db.rollback()
            fail(db, job, exc, permanent=True)
            log.error("job %s (%s) failed permanently: %s", job_id, kind, exc)
        except Exception as exc:  # noqa: BLE001  (any error => retry with backoff)
            db.rollback()
            fail(db, job, exc)
            log.warning("job %s (%s) failed, will retry: %s", job_id, kind, exc)
        return True


def tick(db: Session) -> None:
    """Housekeeping done every few seconds."""
    requeue_stale(db)
    from app.integrations.gmail import schedule_syncs  # local import: optional feature

    schedule_syncs(db)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log.info("worker started (Ctrl+C to stop)")
    last_tick = 0.0
    try:
        while True:
            if time.monotonic() - last_tick > 30:
                with SessionLocal() as db:
                    tick(db)
                last_tick = time.monotonic()
            if not run_once():
                time.sleep(2)
    except KeyboardInterrupt:
        log.info("worker stopped")


if __name__ == "__main__":
    main()
