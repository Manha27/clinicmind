"""ClinicMind as an MCP server, so any MCP client (Claude Desktop, MCP Inspector, ...)
can use the same tools as the built-in agent.

Run (stdio):   python -m mcp_server.server

Safety: the MCP client shows its own approval prompt before running a tool, so here
`allow_external` is True. Tools still require confirm=true for anything that acts outside ClinicMind.
Never print to stdout in this file: stdout carries the protocol.
"""
from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from app.db import SessionLocal
from app.llm import LLMUnavailable
from app.models import Org
from app.tools import TOOLS, ToolContext, run_tool

mcp = MCPServer(
    "clinicmind",
    instructions=(
        "Tools for a clinic's patient-enquiry CRM. Start with search_leads to find a lead id, "
        "then use get_lead_context or search_memories. All data is synthetic demo data."
    ),
)

session_factory = SessionLocal  # tests replace this


def _run(name: str, **args) -> dict:
    with session_factory() as db:
        org = db.scalar(select(Org).order_by(Org.created_at).limit(1))
        if org is None:
            return {"error": "No organisation found. Run: python -m app.seed"}
        ctx = ToolContext(db=db, org=org, allow_external=True)
        try:
            return run_tool(name, {k: v for k, v in args.items() if v is not None}, ctx)
        except LLMUnavailable:
            return {"error": "The AI service is busy. Try again in a minute."}


def _describe(name: str) -> str:
    return TOOLS[name].description  # one source of truth for descriptions


@mcp.tool(description=_describe("search_leads"))
def search_leads(query: str, status: str | None = None, limit: int = 10) -> dict:
    return _run("search_leads", query=query, status=status, limit=limit)


@mcp.tool(description=_describe("get_lead"))
def get_lead(lead_id: str) -> dict:
    return _run("get_lead", lead_id=lead_id)


@mcp.tool(description=_describe("get_lead_context"))
def get_lead_context(lead_id: str, question: str) -> dict:
    return _run("get_lead_context", lead_id=lead_id, question=question)


@mcp.tool(description=_describe("search_memories"))
def search_memories(lead_id: str, query: str, k: int = 5) -> dict:
    return _run("search_memories", lead_id=lead_id, query=query, k=k)


@mcp.tool(description=_describe("list_tasks"))
def list_tasks(lead_id: str | None = None, status: str = "open") -> dict:
    return _run("list_tasks", lead_id=lead_id, status=status)


@mcp.tool(description=_describe("suggest_next_step"))
def suggest_next_step(lead_id: str) -> dict:
    return _run("suggest_next_step", lead_id=lead_id)


@mcp.tool(description=_describe("add_note"))
def add_note(lead_id: str, text: str, idempotency_key: str | None = None) -> dict:
    return _run("add_note", lead_id=lead_id, text=text, idempotency_key=idempotency_key)


@mcp.tool(description=_describe("create_task"))
def create_task(lead_id: str, title: str, due_at: str | None = None) -> dict:
    return _run("create_task", lead_id=lead_id, title=title, due_at=due_at)


@mcp.tool(description=_describe("send_slack_alert"))
def send_slack_alert(text: str, confirm: bool = False) -> dict:
    return _run("send_slack_alert", text=text, confirm=confirm)


@mcp.tool(description=_describe("create_treatment_plan_doc"))
def create_treatment_plan_doc(lead_id: str, confirm: bool = False) -> dict:
    return _run("create_treatment_plan_doc", lead_id=lead_id, confirm=confirm)


if __name__ == "__main__":
    mcp.run()  # stdio transport
