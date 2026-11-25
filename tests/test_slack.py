import httpx
import pytest

from app.config import settings
from app.integrations import http, slack
from app.jobs import queue
from app.models import Job
from app.worker import run_once

URL = "https://hooks.slack.test/T/B/secret"


@pytest.fixture(autouse=True)
def slack_config(monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", URL)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)  # no real waiting in tests


def fake_slack(monkeypatch, handler):
    monkeypatch.setattr(http, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_message_is_posted_as_json(monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), request.read()))
        return httpx.Response(200, text="ok")

    fake_slack(monkeypatch, handler)
    slack.send_message("hello team")
    assert seen[0][0] == URL and b"hello team" in seen[0][1]


def test_rate_limit_is_retried_after_waiting(monkeypatch):
    waits, calls = [], []
    monkeypatch.setattr(http.time, "sleep", lambda s: waits.append(s))

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, text="ok")

    fake_slack(monkeypatch, handler)
    slack.send_message("hi")
    assert len(calls) == 2
    assert 3.0 in waits  # honoured Retry-After


def test_revoked_webhook_is_a_permanent_error(monkeypatch):
    fake_slack(monkeypatch, lambda request: httpx.Response(404, text="no_service"))
    with pytest.raises(http.UpstreamError) as info:
        slack.send_message("hi")
    assert info.value.permanent is True


def test_persistent_server_errors_raise_after_retries(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    fake_slack(monkeypatch, handler)
    with pytest.raises(http.UpstreamError):
        slack.send_message("hi")
    assert len(calls) == 5  # first try + 4 retries


def test_slack_job_goes_dead_when_webhook_is_revoked(db, session_factory, monkeypatch):
    fake_slack(monkeypatch, lambda request: httpx.Response(404))
    job = queue.enqueue(db, "slack_alert", {"text": "hi"}, commit=True)
    run_once(session_factory)
    db.refresh(job)
    assert job.status == "dead" and job.attempts == 1


def test_slack_test_endpoint_queues_a_job(client, db):
    r = client.post("/integrations/slack/test")
    assert r.status_code == 202
    assert db.get(Job, r.json()["job_id"]).kind == "slack_alert"


def test_throttle_spaces_calls(monkeypatch):
    waits = []
    monkeypatch.setattr(http.time, "sleep", lambda s: waits.append(s))
    throttle = http.Throttle(2.0)  # 2 per second => 0.5s apart
    throttle.wait()
    throttle.wait()
    assert waits and 0 < waits[0] <= 0.5
