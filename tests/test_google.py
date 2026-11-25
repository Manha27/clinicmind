import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import settings
from app.integrations import gmail, http
from app.integrations.crypto import decrypt, encrypt
from app.integrations.google_oauth import DOCS_SCOPE, SCOPES
from app.jobs import queue
from app.models import (
    IntegrationAccount, Job, Lead, Memory, Message, OAuthState, Org, SyncCursor, Task, Thread,
)
from app.worker import run_once

T0 = 1_700_000_000_000  # an arbitrary "internalDate" in ms


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def email(mid, thread, sender, to, subject, body, ts=T0, html=False):
    return {
        "id": mid,
        "threadId": thread,
        "internalDate": str(ts),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ],
            "parts": [{"mimeType": "text/html" if html else "text/plain", "body": {"data": b64(body)}}],
        },
    }


class FakeGoogle:
    """Pretends to be Google's token, Gmail and Docs servers."""

    def __init__(self):
        self.messages, self.list_ids = {}, []
        self.history, self.expired_history = {}, set()
        self.profile = {"emailAddress": "clinic@gmail.test", "historyId": "100"}
        self.token_error = None
        self.first_tokens = {"access_token": "first-access", "refresh_token": "refresh-1", "expires_in": 3600, "scope": " ".join(SCOPES)}
        self.token_calls, self.auth_headers, self.docs_calls = [], [], []
        self.fail_once = {}

    def add(self, msg):
        self.messages[msg["id"]] = msg

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, path = request.url, request.url.path
        if url.host == "oauth2.googleapis.com":
            form = parse_qs(request.read().decode())
            self.token_calls.append(form)
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error})
            if form["grant_type"] == ["refresh_token"]:
                return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})
            return httpx.Response(200, json=self.first_tokens)

        if url.host == "docs.googleapis.com":
            self.docs_calls.append((request.method, path, json.loads(request.read() or b"{}")))
            return httpx.Response(200, json={"documentId": "doc123"} if path == "/v1/documents" else {})

        self.auth_headers.append(request.headers.get("authorization"))
        if path.endswith("/profile"):
            return httpx.Response(200, json=self.profile)
        if path.endswith("/history"):
            start = url.params["startHistoryId"]
            if start in self.expired_history:
                return httpx.Response(404, json={"error": {"message": "history too old"}})
            return httpx.Response(200, json=self.history.get(start, {"historyId": start}))
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": i} for i in self.list_ids]})
        mid = path.rsplit("/", 1)[-1]
        if self.fail_once.pop(mid, None):
            return httpx.Response(429, headers={"Retry-After": "2"})
        if mid in self.messages:
            return httpx.Response(200, json=self.messages[mid])
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    google = FakeGoogle()
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "secret")
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(google)))
    return google


def make_account(db, *, expires_in=3600, scopes=SCOPES, refresh="refresh-1"):
    org = db.scalar(select(Org))
    account = IntegrationAccount(
        org_id=org.id, provider="google", account_email="clinic@gmail.test",
        access_token_enc=encrypt("old-access"), refresh_token_enc=encrypt(refresh) if refresh else None,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in), scopes=list(scopes),
    )
    db.add(account)
    db.commit()
    return account


def add_lead(client, mail="Priya@Example.com"):
    return client.post("/leads", json={"email": mail, "full_name": "Priya"}).json()["id"]


# ---------------- OAuth ----------------

def test_connect_redirects_to_google_with_state_and_pkce(client, db, fake):
    r = client.get("/integrations/google/connect", follow_redirects=False)
    assert r.status_code == 307
    location = urlparse(r.headers["location"])
    q = parse_qs(location.query)
    assert location.netloc == "accounts.google.com"
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert q["client_id"] == ["cid"] and q["response_type"] == ["code"]

    verifier = db.get(OAuthState, q["state"][0]).code_verifier
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [expected]  # only the hash went to Google, not the verifier


def test_connect_without_credentials_gives_a_clear_error(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    r = client.get("/integrations/google/connect", follow_redirects=False)
    assert r.status_code == 503 and "GOOGLE_CLIENT_ID" in r.json()["detail"]


def test_callback_exchanges_code_and_stores_encrypted_tokens(client, db, fake):
    r = client.get("/integrations/google/connect", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    verifier = db.get(OAuthState, state).code_verifier

    r = client.get("/integrations/google/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200 and "clinic@gmail.test" in r.text

    form = fake.token_calls[0]
    assert form["code"] == ["abc"] and form["code_verifier"] == [verifier]
    account = db.scalar(select(IntegrationAccount))
    assert account.account_email == "clinic@gmail.test" and account.status == "active"
    assert b"first-access" not in bytes(account.access_token_enc)  # encrypted at rest
    assert decrypt(account.refresh_token_enc) == "refresh-1"

    # the state is single-use, so replaying the callback fails
    assert client.get("/integrations/google/callback", params={"code": "abc", "state": state}).status_code == 400
    assert client.get("/integrations/google/callback", params={"code": "x", "state": "forged"}).status_code == 400


def test_callback_without_refresh_token_is_rejected(client, db, fake):
    fake.first_tokens = {"access_token": "a", "expires_in": 3600}
    r = client.get("/integrations/google/connect", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get("/integrations/google/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400 and "refresh token" in r.json()["detail"]


def test_integrations_list_never_exposes_tokens(client, db, fake):
    make_account(db)
    body = client.get("/integrations").json()
    assert body[0]["account_email"] == "clinic@gmail.test"
    assert not any("token" in key for key in body[0])


# ---------------- Gmail sync ----------------

def test_first_sync_keeps_only_emails_from_known_leads(client, db, fake):
    add_lead(client)  # stored with mixed case on purpose
    fake.add(email("m1", "t1", "Priya <priya@example.com>", "clinic@gmail.test", "Implant cost",
                   "Hi, what is the implant cost?\n\nOn Mon, 3 Nov, Clinic wrote:\n> old quoted text", T0))
    fake.add(email("m2", "t2", "spam@junk.test", "clinic@gmail.test", "Buy now", "spam", T0 + 1000))
    fake.list_ids = ["m2", "m1"]  # Gmail lists newest first

    result = gmail.sync_account(db, make_account(db))

    assert result == {"emails_seen": 2, "messages_stored": 1, "leads_touched": 1}
    message = db.scalar(select(Message))
    assert message.direction == "in" and message.external_id == "m1"
    assert message.body.startswith("Subject: Implant cost") and "implant cost?" in message.body
    assert "wrote" not in message.body and "quoted" not in message.body
    assert message.sent_at == datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    assert db.scalar(select(Thread)).external_thread_id == "t1"
    assert db.get(SyncCursor, (db.scalar(select(IntegrationAccount.id)), "gmail_history")).cursor == "100"
    assert db.scalar(select(Job).where(Job.kind == "extract_lead")) is not None  # extraction queued


def test_incremental_sync_uses_history_and_never_duplicates(client, db, fake):
    add_lead(client)
    fake.add(email("m1", "t1", "priya@example.com", "clinic@gmail.test", "Implant cost", "Hi", T0))
    fake.list_ids = ["m1"]
    account = make_account(db)
    gmail.sync_account(db, account)

    fake.add(email("m3", "t1", "clinic@gmail.test", "priya@example.com", "Re: Implant cost", "Costs start at 60k.", T0 + 5000))
    fake.history["100"] = {"history": [{"messagesAdded": [{"message": {"id": "m3", "threadId": "t1"}}]}], "historyId": "150"}
    result = gmail.sync_account(db, account)

    assert result["messages_stored"] == 1
    out = db.scalar(select(Message).where(Message.external_id == "m3"))
    assert out.direction == "out"  # sent by the clinic account
    assert len(db.scalars(select(Thread)).all()) == 1  # same Gmail thread
    assert db.get(SyncCursor, (account.id, "gmail_history")).cursor == "150"

    # simulate a repeated/rewound sync: nothing is stored twice
    db.get(SyncCursor, (account.id, "gmail_history")).cursor = "100"
    db.commit()
    again = gmail.sync_account(db, account)
    assert again["emails_seen"] == 1 and again["messages_stored"] == 0
    assert len(db.scalars(select(Message)).all()) == 2


def test_expired_history_cursor_triggers_a_full_resync(client, db, fake):
    add_lead(client)
    fake.add(email("m1", "t1", "priya@example.com", "clinic@gmail.test", "Hi", "Hello", T0))
    fake.list_ids = ["m1"]
    account = make_account(db)
    db.add(SyncCursor(account_id=account.id, resource="gmail_history", cursor="5"))
    db.commit()
    fake.expired_history.add("5")

    result = gmail.sync_account(db, account)
    assert result["messages_stored"] == 1
    assert db.get(SyncCursor, (account.id, "gmail_history")).cursor == "100"


def test_rate_limited_message_fetch_is_retried(client, db, fake):
    add_lead(client)
    fake.add(email("m1", "t1", "priya@example.com", "clinic@gmail.test", "Hi", "Hello", T0))
    fake.list_ids = ["m1"]
    fake.fail_once["m1"] = True  # first fetch returns 429
    assert gmail.sync_account(db, make_account(db))["messages_stored"] == 1


def test_html_only_emails_are_converted_to_text(client, db, fake):
    add_lead(client)
    fake.add(email("m1", "t1", "priya@example.com", "clinic@gmail.test", "", "<html><style>p{}</style><p>Can I come at <b>6 pm</b>?</p></html>", T0, html=True))
    fake.list_ids = ["m1"]
    gmail.sync_account(db, make_account(db))
    assert db.scalar(select(Message)).body == "Can I come at 6 pm?"


def test_expired_access_token_is_refreshed_and_used(client, db, fake):
    add_lead(client)
    fake.list_ids = []
    account = make_account(db, expires_in=-60)  # already expired
    gmail.sync_account(db, account)

    assert fake.token_calls[0]["grant_type"] == ["refresh_token"]
    assert fake.token_calls[0]["refresh_token"] == ["refresh-1"]
    assert set(fake.auth_headers) == {"Bearer new-access"}
    db.refresh(account)
    assert decrypt(account.access_token_enc) == "new-access"


def test_revoked_access_marks_account_and_kills_the_job(client, db, fake, session_factory):
    account = make_account(db, expires_in=-60)
    fake.token_error = "invalid_grant"
    job = queue.enqueue(db, "gmail_sync", {"account_id": str(account.id)}, commit=True)

    run_once(session_factory)
    db.refresh(job)
    db.refresh(account)
    assert job.status == "dead" and job.attempts == 1  # retrying cannot help
    assert account.status == "needs_reauth"


def test_sync_endpoint_runs_inline_or_queues(client, db, fake):
    assert client.post("/integrations/google/sync").status_code == 409  # nothing connected
    add_lead(client)
    fake.add(email("m1", "t1", "priya@example.com", "clinic@gmail.test", "Hi", "Hello", T0))
    fake.list_ids = ["m1"]
    make_account(db)

    r = client.post("/integrations/google/sync", params={"inline": "true"})
    assert r.status_code == 202 and r.json()[0]["messages_stored"] == 1

    first = client.post("/integrations/google/sync").json()[0]["job_id"]
    second = client.post("/integrations/google/sync").json()[0]["job_id"]
    assert first == second  # deduplicated while queued


def test_scheduler_queues_due_syncs_only_once(db, fake):
    account = make_account(db)
    gmail.schedule_syncs(db)
    gmail.schedule_syncs(db)
    assert len(db.scalars(select(Job).where(Job.kind == "gmail_sync")).all()) == 1

    job = db.scalar(select(Job))
    job.status = "done"
    db.add(SyncCursor(account_id=account.id, resource="gmail_history", cursor="1", last_synced_at=datetime.now(timezone.utc)))
    db.commit()
    gmail.schedule_syncs(db)  # synced a moment ago, so it is not due
    assert len(db.scalars(select(Job).where(Job.kind == "gmail_sync")).all()) == 1


# ---------------- Google Docs ----------------

def test_treatment_plan_doc_is_created_from_lead_memory(client, db, fake):
    lead_id = add_lead(client)
    lead = db.get(Lead, lead_id)
    db.add(Memory(org_id=lead.org_id, scope="lead", scope_id=lead.id, kind="fact", content="Budget is 80,000 rupees",
                  embedding=[1.0] + [0.0] * (settings.embed_dim - 1)))
    db.add(Task(org_id=lead.org_id, lead_id=lead.id, title="Send EMI options"))
    db.commit()
    make_account(db)

    r = client.post(f"/leads/{lead_id}/treatment-plan-doc")
    assert r.status_code == 200 and r.json()["url"] == "https://docs.google.com/document/d/doc123/edit"
    create, update = fake.docs_calls
    assert create[2]["title"] == "Treatment plan - Priya"
    text = update[2]["requests"][0]["insertText"]["text"]
    assert "Budget is 80,000 rupees" in text and "Send EMI options" in text


def test_doc_needs_a_connected_account_with_docs_permission(client, db, fake):
    lead_id = add_lead(client)
    assert client.post(f"/leads/{lead_id}/treatment-plan-doc").status_code == 409
    make_account(db, scopes=[SCOPES[0]])  # Gmail only
    assert client.post(f"/leads/{lead_id}/treatment-plan-doc").status_code == 409
