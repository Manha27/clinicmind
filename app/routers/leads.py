from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_lead_or_404, get_org
from app.models import Lead, Org
from app.schemas import LeadCreate, LeadOut, LeadStatus, LeadUpdate

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post("", response_model=LeadOut, status_code=201)
def create_lead(
    data: LeadCreate, db: Session = Depends(get_db), org: Org = Depends(get_org)
):
    lead = Lead(org_id=org.id, **data.model_dump())
    db.add(lead)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A lead with this email already exists")
    db.refresh(lead)
    return lead


@router.get("", response_model=list[LeadOut])
def list_leads(
    status: LeadStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    org: Org = Depends(get_org),
):
    stmt = select(Lead).where(Lead.org_id == org.id)
    if status:
        stmt = stmt.where(Lead.status == status)
    stmt = stmt.order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    return db.scalars(stmt).all()


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead: Lead = Depends(get_lead_or_404)):
    return lead


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead(
    data: LeadUpdate,
    lead: Lead = Depends(get_lead_or_404),
    db: Session = Depends(get_db),
):
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(lead, field, value)
    lead.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A lead with this email already exists")
    db.refresh(lead)
    return lead


@router.delete("/{lead_id}", status_code=204)
def delete_lead(lead: Lead = Depends(get_lead_or_404), db: Session = Depends(get_db)):
    db.delete(lead)
    db.commit()