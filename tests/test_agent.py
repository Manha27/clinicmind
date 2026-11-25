from types import SimpleNamespace

import pytest
from google.genai import errors, types
from sqlalchemy import func, select

from app import agent
from app.agent import ModelTurn, ToolCall, declarations, gemini_step, run_agent
from app.config import settings
from app.llm import LLMUnavailable
from app.models import Job, Org, Task
from app.tools import TOOLS


@pytest.fixture
def org(db):
    return db.scalar(select(Org))


def last_result(contents):
    """The most recent tool result the agent sent back to the model."""
    return contents[-1].parts[0].function_response.response["result"]


def test_agent_chains_tools_and_answers(client, db, org):
    client.post("/leads", json={"full_name": "Priya Sharma", "email": "p@example.com"})
    seen_roles = []

    def scripted(contents, system):
        seen_roles.append([c.role for c in contents])
        n = len(seen_roles)
        if n == 1:
            return ModelTurn(None, [ToolCall("search_leads", {"query": "Priya"})])
        if n == 2:
            lead_id = last_result(contents)["leads"][0]["id"]
            return ModelTurn(None, [ToolCall("create_task", {"lead_id": lead_id, "title": "Call Priya"})])
        return ModelTurn("Done: I created a task to call Priya.", [])

    result = run_agent(db, org, "Remind me to call Priya", step=scripted)

    assert result.answer.startswith("Done") and result.stopped == "answered"
    assert [s["tool"] for s in result.steps] == ["search_leads", "create_task"]
    assert db.scalar(select(Task)).title == "Call Priya"
    assert seen_roles[-1][-1] == "user"  # tool results go back to the model as a user turn


def test_model_cannot_approve_its_own_external_actions(client, db, org, monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/x")

    def eager(contents, system):
        if len(contents) == 1:
            return ModelTurn(None, [ToolCall("send_slack_alert", {"text": "hi", "confirm": True})])
        return ModelTurn(f"status={last_result(contents)['status']}", [])

    refused = run_agent(db, org, "tell the team", step=eager)
    assert refused.answer == "status=needs_confirmation"
    assert db.scalar(select(func.count()).select_from(Job)) == 0

    approved = run_agent(db, org, "tell the team", allow_actions=True, step=eager)  # the HUMAN approved
    assert approved.answer == "status=queued"
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_tool_errors_are_fed_back_so_the_model_can_recover(db, org):
    def clumsy(contents, system):
        if len(contents) == 1:
            return ModelTurn(None, [ToolCall("get_lead", {"lead_id": "made-up-id"})])
        return ModelTurn("Sorry, that id was invalid.", [])

    result = run_agent(db, org, "look up the lead", step=clumsy)
    assert "not a valid lead id" in result.steps[0]["result"]["error"]
    assert result.answer.startswith("Sorry")


def test_agent_stops_at_the_step_limit(db, org):
    result = run_agent(db, org, "loop", max_steps=3, step=lambda c, s: ModelTurn(None, [ToolCall("search_leads", {"query": "a"})]))
    assert result.stopped == "step_limit" and len(result.steps) == 3


def test_current_lead_and_history_reach_the_model(db, org):
    captured = {}

    def spy(contents, system):
        captured.update(contents=contents, system=system)
        return ModelTurn("ok", [])

    run_agent(db, org, "and her budget?", lead_id="abc-123", history=[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}], step=spy)
    assert "abc-123" in captured["system"]
    assert [c.role for c in captured["contents"]] == ["user", "model", "user"]


def test_every_tool_becomes_a_valid_gemini_declaration():
    decls = declarations()
    assert {d.name for d in decls} == set(TOOLS)
    task = next(d for d in decls if d.name == "create_task")
    assert set(task.parameters_json_schema["required"]) == {"lead_id", "title"}
    assert "title" in task.parameters_json_schema["properties"]


def fake_response(*, calls=None, text=None):
    content = types.Content(role="model", parts=[types.Part.from_text(text=text or "")])
    return SimpleNamespace(
        response=SimpleNamespace(
            function_calls=[SimpleNamespace(name=n, args=a) for n, a in (calls or [])],
            text=text,
            candidates=[SimpleNamespace(content=content)],
        )
    )


def test_gemini_step_translates_model_output(monkeypatch):
    seen = {}

    def fake_call_model(contents, config, *, purpose, manifest=None):
        seen["config"] = config
        return fake_response(calls=[("search_leads", {"query": "Asha"})])

    monkeypatch.setattr(agent, "call_model", fake_call_model)
    turn = gemini_step([], "system text")
    assert turn.calls[0].name == "search_leads" and turn.calls[0].args == {"query": "Asha"}
    assert turn.text is None and turn.raw is not None
    assert seen["config"].automatic_function_calling.disable is True  # we execute tools ourselves
    assert len(seen["config"].tools[0].function_declarations) == len(TOOLS)

    monkeypatch.setattr(agent, "call_model", lambda *a, **k: fake_response(text="All done"))
    assert gemini_step([], "s").text == "All done"


def test_chat_endpoint_and_tool_listing(client, monkeypatch):
    monkeypatch.setattr(agent, "gemini_step", lambda contents, system: ModelTurn("Hello!", []))
    r = client.post("/agent/chat", json={"message": "hi"})
    assert r.status_code == 200 and r.json() == {"answer": "Hello!", "steps": [], "stopped": "answered"}

    tools = client.get("/agent/tools").json()
    assert {t["name"] for t in tools} == set(TOOLS)
    assert next(t for t in tools if t["name"] == "send_slack_alert")["acts_outside"] is True


def test_chat_returns_503_when_the_model_is_down(client, monkeypatch):
    def down(contents, system):
        raise LLMUnavailable("overloaded")

    monkeypatch.setattr(agent, "gemini_step", down)
    assert client.post("/agent/chat", json={"message": "hi"}).status_code == 503
