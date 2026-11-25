"""Run the evals:   python -m evals.run            (all strategies, all 48 questions)
                   python -m evals.run --leads 2 --strategies memory --sleep 2

Uses your LLM quota: roughly 3 x 48 answer calls plus the memory-building calls.
Use --sleep to stay under free-tier rate limits. Results go to the database, to
evals/output/report.md, report.html and results.json.
"""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import EvalResult, EvalRun, Lead, Memory, MemoryEvent, Org
from evals.fixtures import SCENARIOS, total_questions
from evals.scoring import score
from evals.stats import summarize
from evals.strategies import STRATEGIES


def _cleanup(db: Session, org_id) -> None:
    """Remove everything the eval created (it lives in its own throwaway organisation)."""
    memory_ids = select(Memory.id).where(Memory.org_id == org_id)
    db.execute(delete(MemoryEvent).where(MemoryEvent.memory_id.in_(memory_ids)))
    db.execute(delete(Memory).where(Memory.org_id == org_id))
    db.execute(delete(Lead).where(Lead.org_id == org_id))  # messages and threads cascade
    db.execute(delete(Org).where(Org.id == org_id))
    db.commit()


def _failed(error: str) -> dict:
    return {"answer": None, "correct": False, "stale": False, "error": error[:300],
            "prompt_tokens": None, "completion_tokens": None, "latency_ms": None, "cost_usd": None}


def _ask(strategy, scenario, question, db: Session) -> dict:
    try:
        answer = strategy.answer(scenario, question, db)
    except Exception as exc:  # noqa: BLE001  (a failed call is data, not a crash)
        db.rollback()
        return _failed(f"{type(exc).__name__}: {exc}")
    correct, stale = score(answer.text, question)
    return {"answer": answer.text, "correct": correct, "stale": stale, "error": None,
            "prompt_tokens": answer.prompt_tokens, "completion_tokens": answer.completion_tokens,
            "latency_ms": answer.latency_ms, "cost_usd": answer.cost_usd}


def run_eval(
    strategies: list[str] | None = None,
    session_factory=SessionLocal,
    scenarios: list[dict] | None = None,
    limit_leads: int | None = None,
    sleep_s: float = 0.0,
    keep: bool = False,
    progress=print,
) -> dict[str, dict]:
    scenarios = list(scenarios or SCENARIOS)[: limit_leads or None]
    names = strategies or list(STRATEGIES)
    summaries: dict[str, dict] = {}

    with session_factory() as db:
        org = Org(name=f"eval-{uuid.uuid4().hex[:8]}")
        db.add(org)
        db.commit()
        org_id = org.id
        try:
            for name in names:
                strategy = STRATEGIES[name]()
                run = EvalRun(strategy=name, config={
                    "model": settings.llm_model, "fallback_model": settings.llm_fallback_model,
                    "scenarios": len(scenarios), "questions": total_questions(scenarios),
                })
                db.add(run)
                db.commit()
                run_id = run.id
                rows: list[dict] = []
                setup_total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

                for scenario in scenarios:
                    setup_error = None
                    try:
                        setup = strategy.prepare(scenario, db, db.get(Org, org_id))
                        for key in setup_total:
                            setup_total[key] += setup.get(key, 0)
                    except Exception as exc:  # noqa: BLE001
                        db.rollback()
                        setup_error = f"setup failed: {type(exc).__name__}: {exc}"

                    for index, question in enumerate(scenario["questions"]):
                        row = _failed(setup_error) if setup_error else _ask(strategy, scenario, question, db)
                        db.add(EvalResult(
                            run_id=run_id, case_key=f"{scenario['id']}:{index}", question=question["q"],
                            tags=question["tags"], stale=row["stale"], correct=row["correct"],
                            answer=row["answer"], error=row["error"], prompt_tokens=row["prompt_tokens"],
                            completion_tokens=row["completion_tokens"], latency_ms=row["latency_ms"],
                            cost_usd=row["cost_usd"],
                        ))
                        db.commit()
                        rows.append({**row, "tags": question["tags"]})
                        mark = "ok " if row["correct"] else ("ERR" if row["error"] else "bad")
                        progress(f"[{name}] {scenario['id']}:{index} {mark} {question['q']}")
                        if sleep_s:
                            time.sleep(sleep_s)

                run = db.get(EvalRun, run_id)
                run.summary = summarize(rows, setup_total)
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
                summaries[name] = run.summary
        finally:
            if not keep:
                _cleanup(db, org_id)
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare full-history, summary-only and memory retrieval.")
    parser.add_argument("--strategies", nargs="+", choices=list(STRATEGIES), default=list(STRATEGIES))
    parser.add_argument("--leads", type=int, default=None, help="only the first N scenarios (quick trial)")
    parser.add_argument("--sleep", type=float, default=1.0, help="seconds between questions (rate limits)")
    parser.add_argument("--keep", action="store_true", help="keep the eval leads and memories in the database")
    parser.add_argument("--out", default="evals/output")
    args = parser.parse_args()

    n = min(args.leads or len(SCENARIOS), len(SCENARIOS))
    print(f"Running {len(args.strategies)} strategies on {n} scenarios, model {settings.llm_model}...")
    summaries = run_eval(args.strategies, limit_leads=args.leads, sleep_s=args.sleep, keep=args.keep)

    from evals.report import write_report

    write_report(Path(args.out), summaries)
    print(json.dumps(summaries, indent=2))
    print(f"\nReport written to {args.out}/report.md and report.html")


if __name__ == "__main__":
    main()
