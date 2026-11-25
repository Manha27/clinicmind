from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EvalRun

router = APIRouter(prefix="/evals", tags=["evals"])


def latest_runs(db: Session) -> list[EvalRun]:
    """The most recent finished run of each strategy."""
    runs = db.scalars(
        select(EvalRun).where(EvalRun.finished_at.is_not(None)).order_by(EvalRun.started_at.desc())
    ).all()
    seen, latest = set(), []
    for run in runs:
        if run.strategy not in seen:
            seen.add(run.strategy)
            latest.append(run)
    return latest


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Latest eval results per strategy (run them with: python -m evals.run)."""
    return [
        {"strategy": r.strategy, "started_at": r.started_at, "config": r.config, "summary": r.summary}
        for r in latest_runs(db)
    ]
