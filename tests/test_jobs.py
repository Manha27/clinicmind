from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.config import settings
from app.jobs import handlers, queue
from app.models import Job, Memory
from app.worker import run_once

LEAD = {"email": "priya@example.com", "full_name": "Priya"}


def count(db, **where):
    stmt = select(func.count()).select_from(Job)
    for key, value in where.items():
        stmt = stmt.where(getattr(Job, key) == value)
    return db.scalar(stmt)


@pytest.fixture
def custom_handlers():
    added = []

    def register(kind, fn):
        handlers.HANDLERS[kind] = fn
        added.append(kind)

    yield register
    for kind in added:
        handlers.HANDLERS.pop(kind, None)


def make_due(db, job_id):
    db.execute(update(Job).where(Job.id == job_id).values(run_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    db.commit()


def test_dedupe_keeps_one_queued_job_per_key(db):
    a = queue.enqueue(db, "x", dedupe_key="k")
    b = queue.enqueue(db, "x", dedupe_key="k")
    db.commit()
    assert a.id == b.id
    assert count(db, kind="x") == 1


def test_a_running_job_does_not_block_a_new_one(db):
    queue.enqueue(db, "x", dedupe_key="k", commit=True)
    queue.claim_next(db)  # now running
    queue.enqueue(db, "x", dedupe_key="k", commit=True)
    assert count(db, kind="x") == 2


def test_worker_runs_a_job_and_stores_the_result(db, session_factory, custom_handlers):
    custom_handlers("t_ok", lambda db, payload: {"echo": payload["n"]})
    job = queue.enqueue(db, "t_ok", {"n": 7}, commit=True)

    assert run_once(session_factory) is True
    assert run_once(session_factory) is False  # queue is empty now
    db.refresh(job)
    assert job.status == "done" and job.result == {"echo": 7} and job.attempts == 1


def test_failure_is_retried_later_with_backoff(db, session_factory, custom_handlers):
    def boom(db, payload):
        raise ValueError("nope")

    custom_handlers("t_boom", boom)
    job = queue.enqueue(db, "t_boom", commit=True)

    run_once(session_factory)
    db.refresh(job)
    assert job.status == "queued" and job.attempts == 1
    assert "nope" in job.last_error
    assert job.run_at > datetime.now(timezone.utc)  # waits before the next attempt
    assert run_once(session_factory) is False  # not due yet


def test_job_dies_after_max_attempts(db, session_factory, custom_handlers):
    def boom(db, payload):
        raise ValueError("nope")

    custom_handlers("t_boom", boom)
    job = queue.enqueue(db, "t_boom", max_attempts=2, commit=True)
    run_once(session_factory)
    make_due(db, job.id)
    run_once(session_factory)
    db.refresh(job)
    assert job.status == "dead" and job.attempts == 2


def test_permanent_error_skips_retries(db, session_factory, custom_handlers):
    def bad(db, payload):
        raise queue.PermanentJobError("cannot work")

    custom_handlers("t_perm", bad)
    job = queue.enqueue(db, "t_perm", commit=True)
    run_once(session_factory)
    db.refresh(job)
    assert job.status == "dead" and job.attempts == 1


def test_unknown_job_kind_is_dead_not_a_crash(db, session_factory):
    job = queue.enqueue(db, "no_such_kind", commit=True)
    run_once(session_factory)
    db.refresh(job)
    assert job.status == "dead"


def test_crashed_worker_jobs_are_recovered(db, monkeypatch):
    job = queue.enqueue(db, "x", commit=True)
    queue.claim_next(db)
    db.execute(update(Job).where(Job.id == job.id).values(locked_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db.commit()

    assert queue.requeue_stale(db) == 1
    db.refresh(job)
    assert job.status == "queued"


def test_dead_job_can_be_retried_through_the_api(client, db, session_factory):
    job = queue.enqueue(db, "no_such_kind", commit=True)
    run_once(session_factory)

    r = client.post(f"/jobs/{job.id}/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["attempts"] == 0
    assert client.post(f"/jobs/{job.id}/retry").status_code == 409  # no longer dead
    assert client.get(f"/jobs/{job.id}").status_code == 200
    assert client.get("/jobs", params={"status": "queued"}).json()[0]["id"] == str(job.id)


def test_adding_messages_queues_one_extraction_job(client, db):
    lead_id = client.post("/leads", json=LEAD).json()["id"]
    client.post(f"/leads/{lead_id}/messages", json={"body": "one"})
    client.post(f"/leads/{lead_id}/messages", json={"body": "two"})
    assert count(db, kind="extract_lead") == 1  # deduplicated while queued

    r = client.post(f"/leads/{lead_id}/extract/async")
    assert r.status_code == 202 and r.json()["kind"] == "extract_lead"
    assert count(db, kind="extract_lead") == 1


def test_worker_extracts_memories_and_queues_slack_alert_for_commitments(client, db, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/T/B/x")
    monkeypatch.setattr(
        "app.memory.extract.generate",
        lambda *a, **k: '{"memories": [{"kind": "commitment", "content": "Clinic will call on Friday", "importance": 0.9, "confidence": 0.9}]}',
    )
    monkeypatch.setattr("app.memory.service.embed", lambda texts, **k: [[1.0] + [0.0] * (settings.embed_dim - 1) for _ in texts])

    lead_id = client.post("/leads", json=LEAD).json()["id"]
    client.post(f"/leads/{lead_id}/messages", json={"body": "Please call me Friday", "direction": "in"})

    assert run_once(session_factory) is True
    assert db.scalar(select(func.count()).select_from(Memory)) == 1
    alert = db.scalar(select(Job).where(Job.kind == "slack_alert"))
    assert alert is not None and "Friday" in alert.payload["text"]
