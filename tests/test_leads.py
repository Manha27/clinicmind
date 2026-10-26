from sqlalchemy import func, select

from app.models import Message

LEAD = {"full_name": "Priya Sharma", "email": "priya@example.com"}


def test_create_and_get_lead(client):
    r = client.post("/leads", json=LEAD)
    assert r.status_code == 201
    lead_id = r.json()["id"]

    r = client.get(f"/leads/{lead_id}")
    assert r.status_code == 200
    assert r.json()["email"] == "priya@example.com"
    assert r.json()["status"] == "new"


def test_duplicate_email_returns_409(client):
    assert client.post("/leads", json=LEAD).status_code == 201
    assert client.post("/leads", json=LEAD).status_code == 409


def test_unknown_lead_returns_404(client):
    r = client.get("/leads/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_patch_changes_only_given_fields(client):
    lead_id = client.post("/leads", json=LEAD).json()["id"]
    r = client.patch(f"/leads/{lead_id}", json={"status": "contacted"})
    assert r.status_code == 200
    assert r.json()["status"] == "contacted"
    assert r.json()["full_name"] == "Priya Sharma"


def test_messages_come_back_in_time_order(client):
    lead_id = client.post("/leads", json=LEAD).json()["id"]
    client.post(
        f"/leads/{lead_id}/messages",
        json={"body": "second", "sent_at": "2026-01-02T10:00:00Z"},
    )
    client.post(
        f"/leads/{lead_id}/messages",
        json={"body": "first", "sent_at": "2026-01-01T10:00:00Z"},
    )
    bodies = [m["body"] for m in client.get(f"/leads/{lead_id}/messages").json()]
    assert bodies == ["first", "second"]


def test_deleting_a_lead_deletes_its_messages(client, db):
    lead_id = client.post("/leads", json=LEAD).json()["id"]
    client.post(f"/leads/{lead_id}/messages", json={"body": "hello"})

    assert client.delete(f"/leads/{lead_id}").status_code == 204
    assert client.get(f"/leads/{lead_id}").status_code == 404
    assert db.scalar(select(func.count()).select_from(Message)) == 0