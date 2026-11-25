import hashlib
import hmac
import json

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Job, Lead, Message, WebhookEvent

SECRET = "wa-app-secret"


@pytest.fixture(autouse=True)
def wa_config(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)
    monkeypatch.setattr(settings, "whatsapp_verify_token", "verify-me")


def payload(*messages, name="Asha Rao"):
    return {
        "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "919876543210", "profile": {"name": name}}],
            "messages": list(messages),
        }}]}]
    }


def text_msg(mid="wamid.1", body="Hi, do you do implants?", sender="919876543210"):
    return {"id": mid, "from": sender, "timestamp": "1700000000", "type": "text", "text": {"body": body}}


def post(client, data, secret=SECRET):
    body = json.dumps(data).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": f"sha256={sig}", "Content-Type": "application/json"})


def test_verification_handshake(client):
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"})
    assert ok.status_code == 200 and ok.text == "1234"
    bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1234"})
    assert bad.status_code == 403


def test_inbound_message_creates_lead_message_and_job(client, db):
    r = post(client, payload(text_msg()))
    assert r.status_code == 200 and r.json()["processed"] == 1

    lead = db.scalar(select(Lead))
    assert lead.phone == "+919876543210" and lead.full_name == "Asha Rao" and lead.source == "whatsapp"
    message = db.scalar(select(Message))
    assert message.body == "Hi, do you do implants?" and message.direction == "in"
    assert db.scalar(select(Job).where(Job.kind == "extract_lead")) is not None


def test_existing_lead_is_matched_by_phone_digits(client, db):
    lead_id = client.post("/leads", json={"full_name": "Asha", "phone": "+91 98765 43210"}).json()["id"]
    post(client, payload(text_msg()))
    assert db.scalar(select(func.count()).select_from(Lead)) == 1
    assert len(client.get(f"/leads/{lead_id}/messages").json()) == 1


def test_redelivered_message_is_ignored(client, db):
    assert post(client, payload(text_msg())).json()["processed"] == 1
    again = post(client, payload(text_msg())).json()
    assert again["duplicate"] == 1 and again["processed"] == 0
    assert db.scalar(select(func.count()).select_from(Message)) == 1
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1


def test_bad_signature_is_rejected_and_stores_nothing(client, db):
    assert post(client, payload(text_msg()), secret="wrong").status_code == 401
    assert client.post("/webhooks/whatsapp", json=payload(text_msg())).status_code == 401
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_non_text_messages_are_kept_as_placeholders(client, db):
    post(client, payload({"id": "wamid.2", "from": "919876543210", "timestamp": "1700000001", "type": "image"}))
    assert db.scalar(select(Message)).body == "[image message]"
