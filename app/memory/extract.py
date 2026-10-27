import json
import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm import generate

logger = logging.getLogger("uvicorn.error")

SYSTEM = """You extract durable, sales-relevant memories from a clinic's patient enquiry conversation.

Return ONLY JSON in this shape:
{"memories": [{"kind": "...", "content": "...", "importance": 0.0, "confidence": 0.0}]}

"kind" must be exactly one of these four words: fact, preference, episode, commitment.
- fact: budget, treatment interest, city, who decides, timeline
- preference: preferred time slots, channel, fears, communication style
- episode: something that happened (include the date if given)
- commitment: something either side promised to do (include when, if given)
Treatment interest, budget and who decides are all kind "fact". Never invent any other kind.

Rules:
- content is one short, self-contained sentence about the patient.
- Use only what is stated in the conversation. Never guess or infer, and never add details that were not said (for example, do not turn "evenings" into "weekdays").
- Resolve relative dates such as "tomorrow" or "Friday" using the timestamp in square brackets on that message.
- Write money amounts with the currency (₹ for rupees) when stated or clearly implied.
- Skip greetings and small talk.
- Do not record medical diagnoses or clinical details beyond the treatment they ask about.
- importance (0-1): how much this would matter for a future conversation.
- confidence (0-1): how clearly the conversation states it.
- If nothing is worth remembering, return {"memories": []}."""


class CandidateMemory(BaseModel):
    kind: Literal["fact", "preference", "episode", "commitment"]
    content: str = Field(min_length=3, max_length=300)
    importance: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)


class Extraction(BaseModel):
    memories: list[CandidateMemory]


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    return raw.strip()


def _parse(raw: str) -> list[CandidateMemory]:
    """Validate each memory on its own, so one bad item doesn't discard the rest."""
    data = json.loads(_strip_fences(raw))
    items = data.get("memories", []) if isinstance(data, dict) else data
    good = []
    for item in items:
        try:
            good.append(CandidateMemory.model_validate(item))
        except ValidationError as exc:
            logger.warning("Dropped invalid memory %r: %s", item, exc.errors()[:1])
    return good


def extract_memories(transcript: str) -> list[CandidateMemory]:
    prompt = f"Conversation:\n{transcript}\n\nReturn the JSON."
    for _ in range(2):  # models occasionally return bad JSON, so retry once
        raw = generate(
            prompt, purpose="extract", system=SYSTEM, json_mode=True, temperature=0.0
        )
        try:
            return _parse(raw)
        except (ValueError, TypeError, AttributeError) as exc:
            logger.warning("Extraction output unusable: %s | raw=%r", exc, raw[:500])
    raise ValueError("Model did not return valid JSON")