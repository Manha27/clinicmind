"""Deterministic answer scoring (no LLM judge): reproducible, free, and instant to re-run.

A question has:
  expect : list of groups; every group needs at least one of its variants in the answer
  stale  : outdated values. Mentioning one WITHOUT saying it changed counts as a stale answer
  wrong  : values that make the answer wrong however it is phrased
"""
import re

ABSTAIN = [[
    "don't know", "do not know", "not mentioned", "no information", "not stated", "isn't mentioned",
    "not provided", "unknown", "not available", "doesn't say", "does not say", "not recorded",
    "no record", "not specified", "cannot find", "can't find", "not in the", "no mention",
]]

# words that show the answer is talking about a CHANGE ("was 60k, now 80k"), not stating the old value
HEDGES = (
    "previously", "earlier", "before", "was ", "from ", "changed", "updated", "increased", "raised",
    "instead", "no longer", "used to", "initially", "originally", "lowered", "reduced", "moved", "rescheduled",
)


def normalize(text: str) -> str:
    """Lower-case and write money/number amounts the same way: 80k, 80,000, Rs. 80000, 0.8 lakh -> 80000."""
    t = text.lower().replace("’", "'").replace("₹", " ")
    t = re.sub(r"(?<=\d),(?=\d)", "", t)  # 80,000 -> 80000
    t = re.sub(r"\b(?:rs\.?|inr)\s*(?=\d)", "", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(?:lakhs?|lacs?)\b", lambda m: str(int(round(float(m[1]) * 100000))), t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*k\b", lambda m: str(int(round(float(m[1]) * 1000))), t)
    return re.sub(r"\s+", " ", t).strip()


def contains(normalized_text: str, variant: str) -> bool:
    """Whole-number aware: '60000' must not match inside '160000'."""
    return re.search(r"(?<!\d)" + re.escape(normalize(variant)) + r"(?!\d)", normalized_text) is not None


def score(answer: str, question: dict) -> tuple[bool, bool]:
    """Returns (correct, stale)."""
    a = normalize(answer or "")
    has_expected = all(any(contains(a, v) for v in group) for group in question["expect"])
    stale_mentioned = any(contains(a, v) for v in question.get("stale", []))
    stale = stale_mentioned and not any(h in a for h in HEDGES)
    wrong = any(contains(a, v) for v in question.get("wrong", []))
    return has_expected and not stale and not wrong, stale
