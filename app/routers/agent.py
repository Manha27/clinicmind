from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import agent as agent_module
from app.db import get_db
from app.deps import get_org
from app.llm import LLMUnavailable
from app.models import Org
from app.schemas import AgentRequest, AgentResponse
from app.tools import TOOLS

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/tools")
def list_tools():
    return [
        {"name": t.name, "description": t.description, "changes_data": t.mutating, "acts_outside": t.external}
        for t in TOOLS.values()
    ]


@router.post("/chat", response_model=AgentResponse)
def chat(body: AgentRequest, org: Org = Depends(get_org), db: Session = Depends(get_db)):
    """Ask the assistant something. It may call tools to look things up or take actions."""
    try:
        result = agent_module.run_agent(
            db,
            org,
            body.message,
            lead_id=str(body.lead_id) if body.lead_id else None,
            allow_actions=body.allow_actions,
            history=[t.model_dump() for t in body.history],
        )
    except LLMUnavailable:
        raise HTTPException(503, "AI service is busy. Try again in a minute.")
    return AgentResponse(answer=result.answer, steps=result.steps, stopped=result.stopped)
