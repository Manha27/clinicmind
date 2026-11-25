import asyncio
import json

import pytest
from sqlalchemy import select

import mcp_server.server as server
from app.models import Org, Task
from app.tools import TOOLS


@pytest.fixture(autouse=True)
def use_test_database(session_factory, monkeypatch):
    monkeypatch.setattr(server, "session_factory", session_factory)


def call(name, args):
    result = asyncio.run(server.mcp.call_tool(name, args))
    return json.loads(result.content[0].text)


def test_mcp_exposes_exactly_the_shared_tools():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == set(TOOLS)
    for t in tools:
        assert t.description == TOOLS[t.name].description  # one source of truth


def test_mcp_tool_schemas_have_the_right_required_arguments():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["create_task"].input_schema
    assert set(schema["required"]) == {"lead_id", "title"}
    assert "title" in schema["properties"]


def test_mcp_search_and_get_lead_use_the_real_database(client):
    lead_id = client.post("/leads", json={"full_name": "Priya Sharma", "email": "p@example.com"}).json()["id"]
    found = call("search_leads", {"query": "Priya"})
    assert found["leads"][0]["id"] == lead_id
    assert call("get_lead", {"lead_id": lead_id})["name"] == "Priya Sharma"


def test_mcp_mutating_tool_and_error_handling(client, db):
    lead_id = client.post("/leads", json={"email": "p@example.com"}).json()["id"]
    assert call("create_task", {"lead_id": lead_id, "title": "Call back"})["status"] == "created"
    assert db.scalar(select(Task)).title == "Call back"
    assert "not a valid lead id" in call("get_lead", {"lead_id": "nope"})["error"]


def test_mcp_external_tools_still_require_confirm(client):
    lead_id = client.post("/leads", json={"email": "p@example.com"}).json()["id"]
    assert call("create_treatment_plan_doc", {"lead_id": lead_id})["status"] == "needs_confirmation"
    assert call("send_slack_alert", {"text": "hi"})["status"] == "needs_confirmation"


def test_mcp_reports_a_missing_organisation(session_factory, db):
    from sqlalchemy import text

    with session_factory() as s:
        s.execute(text("TRUNCATE orgs CASCADE"))
        s.commit()
    assert "No organisation" in call("search_leads", {"query": "x"})["error"]
