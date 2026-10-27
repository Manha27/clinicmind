import json
import zlib

from sqlalchemy import select

from app.config import settings
from app.models import Memory


def fake_embed(texts, **kwargs):
    """Same topic -> similarity ~0.86 (related). Same text -> 1.0. Different topic -> ~0."""
    out = []
    for t in texts:
        v = [0.0] * settings.embed_dim
        low = t.lower()
        topic = 0 if "budget" in low else 1 if "needle" in low else 2
        v[topic] = 1.0
        v[10 + zlib.crc32(t.encode()) % 500] = 0.4
        out.append(v)
    return out


def candidates(*contents):
    return json.dumps(
        {
            "memories": [
                {"kind": "fact", "content": c, "importance": 0.8, "confidence": 0.9}
                for c in contents
            ]
        }
    )


def no_llm(*args, **kwargs):
    raise AssertionError("reconcile should not have called the LLM")


def new_lead(client):
    return client.post("/leads", json={"email": "a@example.com"}).json()["id"]


def extract(client, monkeypatch, lead_id, body, memory_text):
    client.post(f"/leads/{lead_id}/messages", json={"body": body})
    monkeypatch.setattr("app.memory.service.embed", fake_embed)
    monkeypatch.setattr("app.memory.extract.generate", lambda *a, **k: candidates(memory_text))
    return client.post(f"/leads/{lead_id}/extract").json()


def contents(client, lead_id, status="active"):
    r = client.get(f"/leads/{lead_id}/memories", params={"status": status})
    return [m["content"] for m in r.json()]


def test_changed_budget_supersedes_old_memory(client, db, monkeypatch):
    lead_id = new_lead(client)
    extract(client, monkeypatch, lead_id, "budget 60k", "Budget is 60k")

    monkeypatch.setattr(
        "app.memory.reconcile.generate",
        lambda *a, **k: '{"op": "SUPERSEDE", "target": 0, "reason": "budget changed"}',
    )
    extract(client, monkeypatch, lead_id, "budget 80k", "Budget is 80k")

    assert contents(client, lead_id) == ["Budget is 80k"]
    assert contents(client, lead_id, "superseded") == ["Budget is 60k"]

    old = db.scalar(select(Memory).where(Memory.content == "Budget is 60k"))
    new = db.scalar(select(Memory).where(Memory.content == "Budget is 80k"))
    assert old.superseded_by == new.id
    assert old.valid_to is not None


def test_identical_memory_is_skipped_without_llm_call(client, monkeypatch):
    lead_id = new_lead(client)
    extract(client, monkeypatch, lead_id, "budget 60k", "Budget is 60k")

    monkeypatch.setattr("app.memory.reconcile.generate", no_llm)
    again = extract(client, monkeypatch, lead_id, "budget again", "Budget is 60k")

    assert again == []
    assert contents(client, lead_id) == ["Budget is 60k"]


def test_unrelated_memory_is_added_without_llm_call(client, monkeypatch):
    lead_id = new_lead(client)
    extract(client, monkeypatch, lead_id, "budget 60k", "Budget is 60k")

    monkeypatch.setattr("app.memory.reconcile.generate", no_llm)
    extract(client, monkeypatch, lead_id, "needles", "Is afraid of needles")

    assert contents(client, lead_id) == ["Budget is 60k", "Is afraid of needles"]


def test_unusable_reconcile_answer_falls_back_to_add(client, monkeypatch):
    lead_id = new_lead(client)
    extract(client, monkeypatch, lead_id, "budget 60k", "Budget is 60k")

    monkeypatch.setattr("app.memory.reconcile.generate", lambda *a, **k: "garbage")
    extract(client, monkeypatch, lead_id, "budget 80k", "Budget is 80k")

    assert contents(client, lead_id) == ["Budget is 60k", "Budget is 80k"]