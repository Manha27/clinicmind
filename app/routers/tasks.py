import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_lead_or_404, get_org
from app.models import Lead, Memory, MemoryEvent, Org, Task
from app.schemas import MemoryEventOut, TaskCreate, TaskOut

router = APIRouter(tags=["tasks"])


@router.post("/leads/{lead_id}/tasks", response_model=TaskOut, status_code=201)
def create_task(
    data: TaskCreate,
    lead: Lead = Depends(get_lead_or_404),
    org: Org = Depends(get_org),
    db: Session = Depends(get_db),
):
    task = Task(org_id=org.id, lead_id=lead.id, title=data.title, due_at=data.due_at)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.get("/leads/{lead_id}/tasks", response_model=list[TaskOut])
def list_tasks(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    return db.scalars(select(Task).where(Task.lead_id == lead.id).order_by(Task.created_at)).all()


@router.post("/tasks/{task_id}/done", response_model=TaskOut)
def complete_task(task_id: uuid.UUID, org: Org = Depends(get_org), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.org_id == org.id))
    if task is None:
        raise HTTPException(404, "Task not found")
    task.status = "done"
    db.commit()
    db.refresh(task)
    return task


@router.get("/leads/{lead_id}/memory-events", response_model=list[MemoryEventOut])
def memory_events(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    """The audit trail: every ADD / SUPERSEDE / NOOP decision made for this lead's memories."""
    stmt = (
        select(MemoryEvent)
        .join(Memory, Memory.id == MemoryEvent.memory_id)
        .where(Memory.scope == "lead", Memory.scope_id == lead.id)
        .order_by(MemoryEvent.id.desc())
        .limit(100)
    )
    return db.scalars(stmt).all()
