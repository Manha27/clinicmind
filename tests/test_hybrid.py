import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.models import Memory, Org

AXES = {"budget": 0, "needle": 1, "implant": 2}


def fake_embed(texts, **kwargs):
    # topic words get their own axis; everything else shares axis 3 (equal similarity)
    out = []
    for text in texts:
        v = [0.0] * settings.embed_dim
        axis = next((i for w, i in AXES.items() if w in text.lower()), 3)
        v[axis] = 1.0
        out.append(v)
    return out


def add_memory(db, lead_id, content, importance=0.5, age_days=0):
    org = db.scalar(select(Org))
    db.add(
        Memory(
            org_id=org.id,
            scope="lead",
            scope_id=uuid.UUID(lead_id),
            kind="fact",
            content=content,
            embedding=fake_embed([content])[0],
            importance=importance,
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )
    )
    db.commit()


def make_lead(client):
    return client.post(
        "/leads",
        json={
            "full_name": "Priya Sharma",
            "email": "p@example.com",
            "profile": {"city": "Jalandhar"},
        },
    ).json()["id"]


def ranked(client, lead_id, q):
    r = client.get(f"/leads/{lead_id}/memories/retrieve", params={"q": q})
    assert r.status_code == 200
    return [h["memory"]["content"] for h in r.json()]


def test_exact_keyword_breaks_a_vector_tie(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    lead_id = make_lead(client)
    add_memory(db, lead_id, "Prefers morning calls")
    add_memory(db, lead_id, "Clinic will send EMI options")
    assert ranked(client, lead_id, "emi options")[0] == "Clinic will send EMI options"


def test_newer_memory_wins_a_tie(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    lead_id = make_lead(client)
    add_memory(db, lead_id, "Likes quiet room", age_days=120)
    add_memory(db, lead_id, "Prefers morning calls", age_days=0)
    assert ranked(client, lead_id, "weather today")[0] == "Prefers morning calls"


def test_more_important_memory_wins_a_tie(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    lead_id = make_lead(client)
    add_memory(db, lead_id, "Likes quiet room", importance=0.2)
    add_memory(db, lead_id, "Prefers morning calls", importance=0.9)
    assert ranked(client, lead_id, "weather today")[0] == "Prefers morning calls"


def test_context_has_sections_and_manifest(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    lead_id = make_lead(client)
    add_memory(db, lead_id, "Budget is 60k")
    client.post(
        f"/leads/{lead_id}/messages", json={"body": "Hello from Priya", "direction": "in"}
    )

    r = client.get(f"/leads/{lead_id}/context", params={"q": "budget"})
    assert r.status_code == 200
    text, manifest = r.json()["text"], r.json()["manifest"]
    assert "## Patient profile" in text
    assert "Name: Priya Sharma" in text
    assert "city: Jalandhar" in text
    assert "Budget is 60k" in text
    assert "Patient: Hello from Priya" in text
    assert len(manifest["memory_ids"]) == 1
    assert len(manifest["message_ids"]) == 1


def test_context_respects_memory_budget(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    lead_id = make_lead(client)
    for i in range(20):
        add_memory(db, lead_id, f"Budget note {i}: " + "x" * 200)

    r = client.get(f"/leads/{lead_id}/context", params={"q": "budget", "k": 20})
    manifest = r.json()["manifest"]
    assert 0 < len(manifest["memory_ids"]) < 20
    assert manifest["tokens"]["memories"] <= 600


def test_ask_sends_memories_to_the_llm(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    seen = {}

    def fake_generate(prompt, **kwargs):
        seen["prompt"] = prompt
        return "About 60k."

    monkeypatch.setattr("app.routers.memories.generate", fake_generate)
    lead_id = make_lead(client)
    add_memory(db, lead_id, "Budget is 60k")

    r = client.post(f"/leads/{lead_id}/ask", json={"question": "What is the budget?"})
    assert r.status_code == 200
    assert r.json()["answer"] == "About 60k."
    assert "Budget is 60k" in seen["prompt"]
