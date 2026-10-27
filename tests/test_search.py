import uuid

from sqlalchemy import select

from app.config import settings
from app.models import Memory, Org

# Fake embeddings: each topic word gets its own axis, so similarity is predictable
AXES = {"budget": 0, "needle": 1, "implant": 2}


def fake_embed(texts, **kwargs):
    out = []
    for text in texts:
        vector = [0.0] * settings.embed_dim
        axis = next((i for word, i in AXES.items() if word in text.lower()), 3)
        vector[axis] = 1.0
        out.append(vector)
    return out


def add_memory(db, lead_id, content, status="active"):
    org = db.scalar(select(Org))
    db.add(
        Memory(
            org_id=org.id,
            scope="lead",
            scope_id=uuid.UUID(lead_id),
            kind="fact",
            content=content,
            embedding=fake_embed([content])[0],
            status=status,
        )
    )
    db.commit()


def new_lead(client, email):
    return client.post("/leads", json={"email": email}).json()["id"]


def test_search_ranks_by_meaning(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    lead_id = new_lead(client, "a@example.com")
    add_memory(db, lead_id, "Budget is 60k")
    add_memory(db, lead_id, "Afraid of needle injections")
    add_memory(db, lead_id, "Wants a dental implant")

    hits = client.get(f"/leads/{lead_id}/memories/search", params={"q": "their budget"}).json()
    assert hits[0]["memory"]["content"] == "Budget is 60k"
    assert hits[0]["similarity"] > 0.99

    hits = client.get(f"/leads/{lead_id}/memories/search", params={"q": "needle fear"}).json()
    assert hits[0]["memory"]["content"] == "Afraid of needle injections"


def test_search_only_returns_active_memories_of_this_lead(client, db, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    lead_a = new_lead(client, "a@example.com")
    lead_b = new_lead(client, "b@example.com")
    add_memory(db, lead_a, "Budget is 60k")
    add_memory(db, lead_a, "Budget is 40k", status="superseded")
    add_memory(db, lead_b, "Budget is 99k")

    hits = client.get(f"/leads/{lead_a}/memories/search", params={"q": "budget", "k": 10}).json()
    assert [h["memory"]["content"] for h in hits] == ["Budget is 60k"]