import uuid
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_lead_or_404, get_org
from app.integrations import gdocs, gmail, google_oauth, http, slack
from app.jobs.queue import enqueue
from app.models import IntegrationAccount, Lead, Org
from app.schemas import IntegrationOut

router = APIRouter(prefix="/integrations", tags=["integrations"])
docs_router = APIRouter(tags=["integrations"])


@router.get("", response_model=list[IntegrationOut])
def list_integrations(org: Org = Depends(get_org), db: Session = Depends(get_db)):
    """Connected accounts. Tokens are never returned."""
    return db.scalars(
        select(IntegrationAccount).where(IntegrationAccount.org_id == org.id)
    ).all()


@router.get("/google/connect")
def google_connect(org: Org = Depends(get_org), db: Session = Depends(get_db)):
    """Open this URL in your BROWSER (not Swagger): it redirects to Google's login page."""
    try:
        return RedirectResponse(google_oauth.start_login(db, org))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@router.get("/google/callback", response_class=HTMLResponse)
def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error or not code or not state:
        raise HTTPException(400, f"Google login failed: {error or 'missing code or state'}")
    try:
        account = google_oauth.complete_login(db, state, code)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except http.UpstreamError as exc:
        raise HTTPException(502, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return HTMLResponse(
        f"<h2>Connected: {escape(account.account_email)}</h2>"
        "<p>You can close this tab. Now call <code>POST /integrations/google/sync</code>.</p>"
    )


@router.post("/google/sync", status_code=202)
def google_sync(
    inline: bool = Query(False, description="Run now in this request instead of queueing"),
    org: Org = Depends(get_org),
    db: Session = Depends(get_db),
):
    accounts = db.scalars(
        select(IntegrationAccount).where(
            IntegrationAccount.org_id == org.id,
            IntegrationAccount.provider == "google",
            IntegrationAccount.status == "active",
        )
    ).all()
    if not accounts:
        raise HTTPException(409, "No connected Google account. Open /integrations/google/connect")
    out = []
    for account in accounts:
        if inline:
            try:
                out.append({"account": account.account_email, **gmail.sync_account(db, account)})
            except http.UpstreamError as exc:
                raise HTTPException(502, str(exc))
        else:
            job = enqueue(db, "gmail_sync", {"account_id": str(account.id)}, dedupe_key=f"gmail_sync:{account.id}")
            db.commit()
            out.append({"account": account.account_email, "job_id": str(job.id)})
    return out


@router.post("/slack/test", status_code=202)
def slack_test(inline: bool = False, db: Session = Depends(get_db)):
    if not settings.slack_webhook_url:
        raise HTTPException(503, "SLACK_WEBHOOK_URL is not set in .env")
    text = "ClinicMind test message"
    if inline:
        try:
            slack.send_message(text)
        except http.UpstreamError as exc:
            raise HTTPException(502, str(exc))
        return {"sent": True}
    job = enqueue(db, "slack_alert", {"text": text})
    db.commit()
    return {"job_id": str(job.id)}


@router.delete("/{account_id}", status_code=204)
def disconnect(account_id: uuid.UUID, org: Org = Depends(get_org), db: Session = Depends(get_db)):
    account = db.scalar(
        select(IntegrationAccount).where(IntegrationAccount.id == account_id, IntegrationAccount.org_id == org.id)
    )
    if account is None:
        raise HTTPException(404, "Account not found")
    db.delete(account)
    db.commit()


@docs_router.post("/leads/{lead_id}/treatment-plan-doc")
def treatment_plan_doc(
    lead: Lead = Depends(get_lead_or_404),
    org: Org = Depends(get_org),
    db: Session = Depends(get_db),
):
    """Create a Google Doc summarising this lead's facts, preferences and follow-ups."""
    try:
        return {"url": gdocs.create_plan_doc(db, org, lead)}
    except google_oauth.NotConnected as exc:
        raise HTTPException(409, str(exc))
    except http.UpstreamError as exc:
        raise HTTPException(502, str(exc))
