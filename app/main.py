from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers import agent, evals, integrations, jobs, leads, memories, messages, tasks, webhooks, whatsapp

app = FastAPI(title="ClinicMind")
app.include_router(leads.router)
app.include_router(messages.router)
app.include_router(webhooks.router)
app.include_router(memories.router)
app.include_router(whatsapp.router)
app.include_router(jobs.router)
app.include_router(tasks.router)
app.include_router(integrations.router)
app.include_router(integrations.docs_router)
app.include_router(agent.router)
app.include_router(evals.router)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": "connected"}


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/ui/")
