from app.config import settings

FAKE_JSON = (
    '{"memories": ['
    '{"kind": "fact", "content": "Budget is about 60,000 rupees", '
    '"importance": 0.9, "confidence": 0.9},'
    '{"kind": "preference", "content": "Is nervous about needles", '
    '"importance": 0.7, "confidence": 0.8}]}'
)


def fake_embed(texts, **kwargs):
    return [[0.1] * settings.embed_dim for _ in texts]


def setup_lead(client):
    lead_id = client.post("/leads", json={"email": "priya@example.com"}).json()["id"]
    client.post(
        f"/leads/{lead_id}/messages",
        json={
            "channel": "whatsapp",
            "direction": "in",
            "body": "Budget is around 60k and I'm nervous about needles.",
        },
    )
    return lead_id


def test_extract_stores_memories_once(client, monkeypatch):
    monkeypatch.setattr("app.memory.extract.generate", lambda *a, **k: FAKE_JSON)
    monkeypatch.setattr("app.memory.service.embed", fake_embed)
    lead_id = setup_lead(client)

    r = client.post(f"/leads/{lead_id}/extract")
    assert r.status_code == 200
    assert len(r.json()) == 2

    # messages are now marked as processed, so a second call does nothing
    assert client.post(f"/leads/{lead_id}/extract").json() == []
    assert len(client.get(f"/leads/{lead_id}/memories").json()) == 2


def test_bad_json_from_model_returns_502(client, monkeypatch):
    monkeypatch.setattr("app.memory.extract.generate", lambda *a, **k: "not json")
    monkeypatch.setattr("app.memory.service.embed", fake_embed)
    lead_id = setup_lead(client)

    assert client.post(f"/leads/{lead_id}/extract").status_code == 502
    assert client.get(f"/leads/{lead_id}/memories").json() == []