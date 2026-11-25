import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Job, Lead, Memory, Message, Org, Task
from app.tools import TOOLS, ToolContext, json_schema, run_tool

UNIT = [1.0] + [0.0] * (settings.embed_dim - 1)


def fake_embed(texts, **kwargs):
    return [UNIT for _ in texts]


@pytest.fixture
def ctx(db):
    return ToolContext(db=db, org=db.scalar(select(Org)))


@pytest.fixture
def priya(client):
    return client.post("/leads", json={"full_name": "Priya Sharma", "email": "priya@example.com", "phone": "+91 98765 43210", "profile": {"city": "Jalandhar"}}).json()["id"]


def add_memory(db, lead_id, content, kind="fact"):
    lead = db.get(Lead, lead_id)
    db.add(Memory(org_id=lead.org_id, scope="lead", scope_id=lead.id, kind=kind, content=content, embedding=UNIT))
    db.commit()


def test_every_required_argument_is_defined_in_the_schema():
    # regression: a field literally named "title" used to be stripped from the schema
    for spec in TOOLS.values():
        schema = json_schema(spec)
        assert set(schema.get("required", [])) <= set(schema["properties"]), spec.name
    assert "title" in json_schema(TOOLS["create_task"])["properties"]


def test_search_and_get_lead(ctx, priya):
    found = run_tool("search_leads", {"query": "priya"}, ctx)["leads"]
    assert [l["name"] for l in found] == ["Priya Sharma"]
    assert run_tool("search_leads", {"query": "98765"}, ctx)["leads"][0]["id"] == priya  # by phone
    assert run_tool("search_leads", {"query": "priya", "status": "won"}, ctx)["leads"] == []

    lead = run_tool("get_lead", {"lead_id": priya}, ctx)
    assert lead["name"] == "Priya Sharma" and lead["profile"] == {"city": "Jalandhar"}
    assert lead["messages"] == 0 and lead["open_tasks"] == 0


def test_bad_input_comes_back_as_an_error_the_model_can_read(ctx, priya):
    assert "not a valid lead id" in run_tool("get_lead", {"lead_id": "abc"}, ctx)["error"]
    assert run_tool("get_lead", {"lead_id": "00000000-0000-0000-0000-000000000000"}, ctx)["error"] == "Lead not found"
    assert "Invalid arguments" in run_tool("search_leads", {}, ctx)["error"]
    assert "Invalid arguments" in run_tool("search_leads", {"query": "x", "limit": 999}, ctx)["error"]
    assert "Unknown tool" in run_tool("delete_everything", {}, ctx)["error"]


def test_other_organisations_leads_are_invisible(db, ctx):
    other = Org(name="Other Clinic")
    db.add(other)
    db.flush()
    lead = Lead(org_id=other.id, full_name="Secret Person", email="s@example.com")
    db.add(lead)
    db.commit()
    assert run_tool("get_lead", {"lead_id": str(lead.id)}, ctx)["error"] == "Lead not found"
    assert run_tool("search_leads", {"query": "Secret"}, ctx)["leads"] == []


def test_memory_tools_use_hybrid_retrieval(db, ctx, priya, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    add_memory(db, priya, "Budget is 80,000 rupees")
    add_memory(db, priya, "Husband decides", kind="fact")

    hits = run_tool("search_memories", {"lead_id": priya, "query": "budget"}, ctx)["memories"]
    assert hits[0]["content"] == "Budget is 80,000 rupees" and "score" in hits[0]
    context = run_tool("get_lead_context", {"lead_id": priya, "question": "budget"}, ctx)
    assert "Budget is 80,000 rupees" in context["context"] and context["tokens"] > 0


def test_suggest_next_step_calls_the_llm_with_context(db, ctx, priya, monkeypatch):
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    seen = {}
    monkeypatch.setattr("app.tools.generate", lambda prompt, **kw: seen.update(prompt=prompt) or "Call her on Friday.")
    add_memory(db, priya, "Wants a call on Friday evening")
    assert run_tool("suggest_next_step", {"lead_id": priya}, ctx) == {"suggestion": "Call her on Friday."}
    assert "Friday evening" in seen["prompt"]


def test_add_note_is_idempotent_and_queues_extraction(db, ctx, priya):
    args = {"lead_id": priya, "text": "Prefers Dr. Mehta", "idempotency_key": "k1"}
    assert run_tool("add_note", args, ctx)["status"] == "saved"
    assert run_tool("add_note", args, ctx)["status"] == "duplicate"
    assert db.scalar(select(func.count()).select_from(Message)) == 1
    assert db.scalar(select(Message)).direction == "internal"
    assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "extract_lead")) == 1


def test_create_task_does_not_duplicate_open_tasks(db, ctx, priya):
    first = run_tool("create_task", {"lead_id": priya, "title": "Send EMI options", "due_at": "2026-11-10T10:00:00Z"}, ctx)
    again = run_tool("create_task", {"lead_id": priya, "title": "send emi options"}, ctx)
    assert first["status"] == "created" and again["status"] == "already_exists"
    assert db.scalar(select(Task)).created_by == "agent"
    tasks = run_tool("list_tasks", {"lead_id": priya}, ctx)["tasks"]
    assert [t["title"] for t in tasks] == ["Send EMI options"]
    assert run_tool("list_tasks", {"lead_id": priya, "status": "done"}, ctx)["tasks"] == []


def test_actions_outside_the_system_need_human_approval(db, priya, monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/x")
    org = db.scalar(select(Org))
    args = {"text": "Hot lead!", "confirm": True}  # the model claims it was approved...

    blocked = run_tool("send_slack_alert", args, ToolContext(db=db, org=org, allow_external=False))
    assert blocked["status"] == "needs_confirmation"  # ...but only the human can approve
    assert db.scalar(select(func.count()).select_from(Job)) == 0

    unconfirmed = run_tool("send_slack_alert", {"text": "Hot lead!"}, ToolContext(db=db, org=org, allow_external=True))
    assert unconfirmed["status"] == "needs_confirmation"

    sent = run_tool("send_slack_alert", args, ToolContext(db=db, org=org, allow_external=True))
    assert sent["status"] == "queued"
    assert db.scalar(select(Job).where(Job.kind == "slack_alert")).payload == {"text": "Hot lead!"}


def test_slack_and_docs_report_missing_setup(db, priya, monkeypatch):
    approved = ToolContext(db=db, org=db.scalar(select(Org)), allow_external=True)
    monkeypatch.setattr(settings, "slack_webhook_url", "")
    assert "not configured" in run_tool("send_slack_alert", {"text": "x", "confirm": True}, approved)["error"]
    assert "No connected Google account" in run_tool("create_treatment_plan_doc", {"lead_id": priya, "confirm": True}, approved)["error"]
    assert run_tool("create_treatment_plan_doc", {"lead_id": priya}, approved)["status"] == "needs_confirmation"
