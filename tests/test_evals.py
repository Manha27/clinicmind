import json
import zlib
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.llm import LLMUnavailable
from app.models import EvalResult, EvalRun, Lead, Memory, Org
from evals import report
from evals.fixtures import SCENARIOS, total_questions, when
from evals.run import run_eval
from evals.scoring import ABSTAIN, contains, normalize, score
from evals.stats import percentile, summarize


# ---------------- scoring ----------------

def test_amounts_are_normalised_so_formats_match():
    for text in ("₹80,000", "80k", "Rs. 80000", "INR 80000", "0.8 lakh", "80K"):
        assert normalize(text) == "80000", text
    assert normalize("1.2 lakh") == "120000" and normalize("1.5 lakhs") == "150000"


def test_numbers_match_whole_numbers_only():
    assert contains(normalize("budget is 160000"), "60000") is False
    assert contains(normalize("budget is 60k"), "60000") is True


QUESTION = {"expect": [["80000"]], "stale": ["60000"], "wrong": []}


@pytest.mark.parametrize("answer,correct,stale", [
    ("Her budget is ₹80,000.", True, False),
    ("She can spend 80k now.", True, False),
    ("Her budget is 60k.", False, True),  # old value stated as current
    ("It was 60k but is now 80k.", True, False),  # mentions the change, so it is not stale
    ("Her budget increased from 60,000 to 80,000.", True, False),
    ("I don't know.", False, False),
])
def test_stale_answers_are_detected(answer, correct, stale):
    assert score(answer, QUESTION) == (correct, stale)


def test_wrong_values_always_fail():
    assert score("Budget 80k and 70k", {"expect": [["80000"]], "wrong": ["70000"]}) == (False, False)


@pytest.mark.parametrize("answer", ["I don't know.", "That is not mentioned in the context.", "There is no information about that."])
def test_abstaining_is_recognised(answer):
    assert score(answer, {"expect": ABSTAIN})[0] is True


def test_inventing_an_answer_fails_an_abstain_question():
    assert score("Her blood group is O positive.", {"expect": ABSTAIN})[0] is False


# ---------------- the dataset itself ----------------

def test_fixtures_are_well_formed():
    assert len({s["id"] for s in SCENARIOS}) == len(SCENARIOS)
    assert total_questions() >= 40
    for s in SCENARIOS:
        stamps = [when(m[0]) for m in s["messages"]]
        assert stamps == sorted(stamps), f"{s['id']} messages are not in order"
        assert {m[1] for m in s["messages"]} <= {"in", "out"}
        assert any("abstain" in q["tags"] for q in s["questions"]), f"{s['id']} has no abstain question"


def test_question_texts_are_unique_across_scenarios():
    texts = [q["q"] for s in SCENARIOS for q in s["questions"]]
    assert len(texts) == len(set(texts)), "reword duplicated questions (a name in the question is enough)"


def test_every_gold_answer_and_stale_value_really_appears_in_the_conversation():
    """A question whose answer is not in the chat would be unfair; a stale value not in it would not be a trap."""
    for s in SCENARIOS:
        chat = normalize(" ".join(m[2] for m in s["messages"]))
        for q in s["questions"]:
            if "abstain" not in q["tags"]:
                for group in q["expect"]:
                    assert any(contains(chat, v) for v in group), f"{s['id']}: {q['q']!r} -> {group}"
            if q["stale"]:  # the stale values are alternatives: at least one must be a real old value
                assert any(contains(chat, old) for old in q["stale"]), f"{s['id']}: no stale value in chat for {q['q']!r}"


def gold_answer(q):
    return "I don't know." if "abstain" in q["tags"] else " and ".join(g[0] for g in q["expect"])


def test_gold_answers_score_as_correct_and_stale_ones_do_not():
    for s in SCENARIOS:
        for q in s["questions"]:
            assert score(gold_answer(q), q)[0] is True, q["q"]
            for old in q["stale"]:
                assert score(f"It is {old}.", q) == (False, True), q["q"]


# ---------------- stats ----------------

def test_percentiles_use_nearest_rank():
    values = list(range(1, 11))
    assert percentile(values, 50) == 5 and percentile(values, 95) == 10
    assert percentile([7], 95) == 7 and percentile([], 50) is None


def test_summary_counts_accuracy_errors_and_tags():
    rows = [
        {"correct": True, "stale": False, "tags": ["update_trap"], "prompt_tokens": 100, "completion_tokens": 5, "latency_ms": 200, "cost_usd": 0.001, "error": None},
        {"correct": False, "stale": True, "tags": ["update_trap"], "prompt_tokens": 300, "completion_tokens": 5, "latency_ms": 400, "cost_usd": 0.003, "error": None},
        {"correct": False, "stale": False, "tags": [], "prompt_tokens": None, "completion_tokens": None, "latency_ms": None, "cost_usd": None, "error": "boom"},
    ]
    s = summarize(rows, {"prompt_tokens": 50, "completion_tokens": 10, "calls": 2})
    assert (s["questions"], s["correct"], s["errors"]) == (3, 1, 1)
    assert s["accuracy"] == pytest.approx(0.3333, abs=1e-3) and s["stale_rate"] == pytest.approx(0.3333, abs=1e-3)
    assert s["avg_prompt_tokens"] == 200 and s["p95_latency_ms"] == 400
    assert s["accuracy_by_tag"] == {"update_trap": 0.5}
    assert s["setup_prompt_tokens"] == 50 and s["setup_calls"] == 2


# ---------------- strategies end to end, with a fake model ----------------

ALL = {q["q"]: q for s in SCENARIOS for q in s["questions"]}


class FakeLLM:
    """Answers every question correctly and records the prompts it was given."""

    def __init__(self):
        self.prompts = []
        self.fail_on = set()

    def __call__(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if "Write the summary." in prompt:
            return SimpleNamespace(text="SUMMARY-TEXT", prompt_tokens=300, completion_tokens=50)
        asked = prompt.rsplit("Question: ", 1)[1]
        if asked in self.fail_on:
            raise LLMUnavailable("down")
        return SimpleNamespace(text=gold_answer(ALL[asked]), prompt_tokens=len(prompt) // 4, completion_tokens=5)


def fake_embed(texts, **kwargs):
    out = []
    for t in texts:
        v = [0.0] * settings.embed_dim
        v[zlib.crc32(t.encode()) % settings.embed_dim] = 1.0
        out.append(v)
    return out


@pytest.fixture
def fakes(monkeypatch):
    llm = FakeLLM()
    extract_calls = []

    def fake_extract(prompt, **kwargs):
        extract_calls.append(prompt)
        return json.dumps({"memories": [{"kind": "fact", "content": f"Fact number {len(extract_calls)}", "importance": 0.8, "confidence": 0.9}]})

    monkeypatch.setattr("evals.strategies.generate_full", llm)
    monkeypatch.setattr("app.memory.extract.generate", fake_extract)
    monkeypatch.setattr("app.memory.service.embed", fake_embed)
    monkeypatch.setattr("app.memory.retrieve.embed", fake_embed)
    llm.extract_calls = extract_calls
    return llm


def test_all_three_strategies_run_and_are_scored(db, session_factory, fakes):
    quiet = []
    summaries = run_eval(session_factory=session_factory, limit_leads=1, progress=quiet.append)

    assert set(summaries) == {"full_history", "summary_only", "memory"}
    for s in summaries.values():
        assert s["questions"] == 6 and s["accuracy"] == 1.0 and s["errors"] == 0
    assert db.scalar(select(func.count()).select_from(EvalResult)) == 18
    assert len(quiet) == 18

    # what each strategy actually put in the prompt
    full = next(p for p in fakes.prompts if p.startswith("Conversation:") and "Write the summary." not in p)
    assert "80k" in full and "60k" in full  # the whole history, old value included
    summary_prompts = [p for p in fakes.prompts if p.startswith("Summary of the conversation:")]
    assert summary_prompts and all("SUMMARY-TEXT" in p and "needles" not in p for p in summary_prompts)
    memory_prompts = [p for p in fakes.prompts if "## What we remember" in p]
    assert memory_prompts and all("Fact number" in p for p in memory_prompts)

    # the memory strategy replayed Priya's conversation one day at a time (3 days => 3 extractions)
    assert len(fakes.extract_calls) == 3
    assert summaries["summary_only"]["setup_prompt_tokens"] == 300


def test_eval_leaves_nothing_behind_unless_asked(db, session_factory, fakes):
    run_eval(strategies=["memory"], session_factory=session_factory, limit_leads=1, progress=lambda m: None)
    assert db.scalar(select(func.count()).select_from(Lead)) == 0
    assert db.scalar(select(func.count()).select_from(Memory)) == 0
    assert db.scalar(select(func.count()).select_from(Org).where(Org.name.like("eval-%"))) == 0
    assert db.scalar(select(func.count()).select_from(EvalRun)) == 1  # the results are kept

    run_eval(strategies=["memory"], session_factory=session_factory, limit_leads=1, keep=True, progress=lambda m: None)
    assert db.scalar(select(func.count()).select_from(Lead)) == 1
    assert db.scalar(select(func.count()).select_from(Memory)) == 3


def test_a_failed_question_is_recorded_as_an_error_not_a_crash(db, session_factory, fakes):
    fakes.fail_on = {"What is Priya's current budget?"}
    summaries = run_eval(strategies=["full_history"], session_factory=session_factory, limit_leads=1, progress=lambda m: None)
    s = summaries["full_history"]
    assert s["errors"] == 1 and s["correct"] == 5 and s["accuracy"] == pytest.approx(5 / 6, abs=1e-3)
    bad = db.scalar(select(EvalResult).where(EvalResult.error.is_not(None)))
    assert "LLMUnavailable" in bad.error and bad.correct is False


def test_a_failed_memory_build_marks_that_scenarios_questions_as_errors(db, session_factory, fakes, monkeypatch):
    monkeypatch.setattr("app.memory.extract.generate", lambda *a, **k: "not json")
    s = run_eval(strategies=["memory"], session_factory=session_factory, limit_leads=1, progress=lambda m: None)["memory"]
    assert s["errors"] == 6 and s["accuracy"] == 0.0


# ---------------- reports and the API ----------------

def test_report_files_and_summary_endpoint(client, db, session_factory, fakes, tmp_path):
    summaries = run_eval(session_factory=session_factory, limit_leads=1, progress=lambda m: None)

    report.write_report(tmp_path, summaries)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "full_history" in md and "summary_only" in md and "memory" in md and "100%" in md
    assert page.count("<svg") == 4 and "prefers-color-scheme" in page
    assert json.loads((tmp_path / "results.json").read_text())["memory"]["questions"] == 6

    api = client.get("/evals/summary").json()
    assert {r["strategy"] for r in api} == {"full_history", "summary_only", "memory"}
    assert api[0]["summary"]["accuracy"] == 1.0
