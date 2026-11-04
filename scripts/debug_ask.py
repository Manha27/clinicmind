"""Diagnose why one /ask question fails.

Calls Gemini directly, ONE attempt per variant (no retries), and prints the real
error, how long it took, and why the model stopped. Stores nothing.

Run from the project root, with the venv active:
    python -m scripts.debug_ask
    python -m scripts.debug_ask "Some other question?"
"""
import sys
import time

from google.genai import types
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.llm import get_client
from app.memory.context import build_context
from app.models import Lead
from app.routers.memories import ANSWER_SYSTEM

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "What is her blood group?"


def attempt(label: str, prompt: str, system: str | None) -> None:
    print(f"\n=== {label} ===")
    start = time.perf_counter()
    try:
        response = get_client().models.generate_content(
            model=settings.llm_model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.2),
        )
        secs = time.perf_counter() - start
        candidate = response.candidates[0] if response.candidates else None
        print(f"OK in {secs:.1f}s")
        print("finish_reason:", getattr(candidate, "finish_reason", None))
        print("prompt_feedback:", getattr(response, "prompt_feedback", None))
        print("text:", (response.text or "(empty)")[:300])
    except Exception as exc:  # noqa: BLE001  (we want to see everything here)
        secs = time.perf_counter() - start
        print(f"FAILED after {secs:.1f}s")
        print("type:", type(exc).__name__)
        print("code:", getattr(exc, "code", None))
        print("message:", str(exc)[:400])


with SessionLocal() as db:
    lead = db.scalar(select(Lead).where(Lead.email == "priya@example.com"))
    if lead is None:
        sys.exit("Priya (priya@example.com) not found. Check GET /leads.")
    ctx = build_context(db, lead, QUESTION)

print("Model:", settings.llm_model, "| timeout:", settings.llm_timeout_s, "s")
print("Question:", QUESTION)
print("Context size (estimated tokens):", ctx.manifest["tokens"]["total"])

# A: exactly what /ask sends
attempt("A: same as /ask (context + question + system)", f"{ctx.text}\n\nQuestion: {QUESTION}", ANSWER_SYSTEM)

# B: no context, no system prompt: is the question itself the trigger?
attempt("B: question only", QUESTION, None)

# C: same context, a differently worded question
attempt(
    "C: context + reworded question",
    f"{ctx.text}\n\nQuestion: Does the context say anything about her blood type? Answer yes or no.",
    ANSWER_SYSTEM,
)
