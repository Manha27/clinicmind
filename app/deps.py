import uuid

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Lead, Org


def get_org(db: Session = Depends(get_db)) -> Org:
    org = db.scalar(select(Org).order_by(Org.created_at).limit(1))
    if org is None:
        raise HTTPException(500, "No org found. Run: python -m app.seed")
    return org


def get_lead_or_404(
    lead_id: uuid.UUID,
    db: Session = Depends(get_db),
    org: Org = Depends(get_org),
) -> Lead:
    lead = db.scalar(select(Lead).where(Lead.id == lead_id, Lead.org_id == org.id))
    if lead is None:
        raise HTTPException(404, "Lead not found")
    return lead