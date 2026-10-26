import hashlib
import hmac
import json
import time

from sqlalchemy import func, select

from app.config import settings
from app.models import Message, WebhookEvent


def make_event(event_id="evt_1", email="rahul@example.com"):
    return {
        "event_id": event_id,
        "name": "Rahul Verma",
        "email": email,
        "phone": "+91 99999 11111",
        "treatment": "teeth whitening",
        "city": "Mohali",
        "message": "What is the cost of whitening?",
    }


def signed_headers(body: bytes, *, timestamp=None, secret=None):
    ts = str(int(time.time())) if timestamp is None else str(timestamp)
    key = (secret or settings.webhook_secret).encode()
    sig = hmac.new(key, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": f"sha256={sig}",
    }


def post_event(client, payload, **kwargs):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/portal", content=body, headers=signed_headers(body, **kwargs)
    )


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_valid_event_creates_lead(client):
    r = post_event(client, make_event())
    assert r.status_code == 200
    assert r.json()["status"] == "processed"

    leads = client.get("/leads").json()
    assert len(leads) == 1
    assert leads[0]["source"] == "portal_webhook"
    assert leads[0]["profile"]["treatment_interest"] == "teeth whitening"


def test_duplicate_event_is_ignored(client, db):
    assert post_event(client, make_event()).json()["status"] == "processed"
    assert post_event(client, make_event()).json()["status"] == "duplicate"

    assert count(db, WebhookEvent) == 1
    assert count(db, Message) == 1


def test_same_email_reuses_lead(client):
    post_event(client, make_event("evt_1"))
    post_event(client, make_event("evt_2", email="RAHUL@example.com"))

    leads = client.get("/leads").json()
    assert len(leads) == 1
    messages = client.get(f"/leads/{leads[0]['id']}/messages").json()
    assert len(messages) == 2


def test_wrong_secret_rejected(client):
    r = post_event(client, make_event(), secret="not-the-secret")
    assert r.status_code == 401


def test_tampered_body_rejected(client):
    body = json.dumps(make_event()).encode()
    headers = signed_headers(body)
    tampered = body.replace(b"whitening", b"surgery!!")
    r = client.post("/webhooks/portal", content=tampered, headers=headers)
    assert r.status_code == 401


def test_old_timestamp_rejected(client):
    r = post_event(client, make_event(), timestamp=int(time.time()) - 600)
    assert r.status_code == 401
    assert "old" in r.json()["detail"]


def test_missing_headers_rejected(client):
    r = client.post("/webhooks/portal", json=make_event())
    assert r.status_code == 401


def test_rejected_requests_are_not_stored(client, db):
    post_event(client, make_event("evt_bad"), secret="not-the-secret")
    assert count(db, WebhookEvent) == 0