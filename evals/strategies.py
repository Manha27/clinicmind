"""The three ways of giving an LLM a patient's history, compared by the evals.

  full_history : paste the whole conversation into every prompt (simple, expensive as chats grow)
  summary_only : summarise once, answer from the summary (cheap, loses details)
  memory       : ClinicMind's pipeline: extract -> reconcile -> hybrid retrieval -> token-budgeted context
"""
import time
import uuid
from dataclasses import dataclass
from itertools import groupby

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm import estimate_cost, generate_full
from app.memory.context import build_context
from app.memory.service import extract_pending
from app.models import Lead, LLMCall, Message, Org, Thread
from app.routers.memories import ANSWER_SYSTEM
from evals.fixtures import when

LABEL = {"in": "Patient", "out": "Clinic"}

SUMMARY_SYSTEM = (
    "You summarise a clinic's conversation with one patient so a colleague can continue it. "
    "Keep every concrete detail that could matter later: names, amounts, dates, times, preferences, "
    "decisions, promises. Use only what was said. At most 150 words."
)


@dataclass
class Answer:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cost_usd: float


def transcript(scenario: dict) -> str:
    return "\n".join(
        f"[{when(t):%Y-%m-%d %H:%M}] {LABEL[direction]}: {text}"
        for t, direction, text in scenario["messages"]
    )


def _answer(prompt: str, started: float) -> Answer:
    """started: perf_counter() taken BEFORE any retrieval work, so latency is end to end."""
    result = generate_full(prompt, purpose="eval_answer", system=ANSWER_SYSTEM, temperature=0.0)
    return Answer(
        text=result.text,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=int((time.perf_counter() - started) * 1000),
        cost_usd=estimate_cost(result.prompt_tokens, result.completion_tokens),
    )


class FullHistory:
    name = "full_history"

    def prepare(self, scenario: dict, db: Session, org: Org) -> dict:
        return {}

    def answer(self, scenario: dict, question: dict, db: Session) -> Answer:
        started = time.perf_counter()
        return _answer(f"Conversation:\n{transcript(scenario)}\n\nQuestion: {question['q']}", started)


class SummaryOnly:
    name = "summary_only"

    def __init__(self):
        self.summaries: dict[str, str] = {}

    def prepare(self, scenario: dict, db: Session, org: Org) -> dict:
        result = generate_full(
            f"Conversation:\n{transcript(scenario)}\n\nWrite the summary.",
            purpose="eval_summary", system=SUMMARY_SYSTEM, temperature=0.0,
        )
        self.summaries[scenario["id"]] = result.text
        return {"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens, "calls": 1}

    def answer(self, scenario: dict, question: dict, db: Session) -> Answer:
        started = time.perf_counter()
        return _answer(f"Summary of the conversation:\n{self.summaries[scenario['id']]}\n\nQuestion: {question['q']}", started)


class MemoryStrategy:
    name = "memory"

    def __init__(self):
        self.leads: dict[str, uuid.UUID] = {}
        self.token = uuid.uuid4().hex[:8]

    def prepare(self, scenario: dict, db: Session, org: Org) -> dict:
        """Replay the conversation day by day, extracting memories after each day,
        exactly as the worker would while messages arrive. Later days can supersede earlier facts."""
        t0 = db.scalar(select(func.clock_timestamp()))
        lead = Lead(org_id=org.id, full_name=scenario["name"], email=f"{scenario['id']}-{self.token}@eval.test", source="eval")
        db.add(lead)
        db.flush()
        thread = Thread(lead_id=lead.id, channel="whatsapp")
        db.add(thread)
        db.commit()
        self.leads[scenario["id"]] = lead.id

        for _, day in groupby(scenario["messages"], key=lambda m: m[0][:10]):
            for stamp, direction, text in day:
                db.add(Message(thread_id=thread.id, lead_id=lead.id, direction=direction, body=text, sent_at=when(stamp)))
            db.commit()
            extract_pending(db, lead)

        row = db.execute(
            select(
                func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
                func.coalesce(func.sum(LLMCall.completion_tokens), 0),
                func.count(),
            ).where(LLMCall.created_at >= t0, LLMCall.purpose.in_(("extract", "reconcile", "embed")))
        ).one()
        return {"prompt_tokens": int(row[0]), "completion_tokens": int(row[1]), "calls": int(row[2])}

    def answer(self, scenario: dict, question: dict, db: Session) -> Answer:
        started = time.perf_counter()
        lead = db.get(Lead, self.leads[scenario["id"]])
        ctx = build_context(db, lead, question["q"])
        return _answer(f"{ctx.text}\n\nQuestion: {question['q']}", started)


STRATEGIES = {cls.name: cls for cls in (FullHistory, SummaryOnly, MemoryStrategy)}
