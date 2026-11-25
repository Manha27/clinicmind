"""Google Docs: generate a treatment-plan summary document for a lead."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations import google_oauth, http
from app.models import Lead, Memory, Task

DOCS_API = "https://docs.googleapis.com/v1/documents"
_throttle = http.Throttle(2.0)


def build_plan(db: Session, lead: Lead) -> tuple[str, str]:
    """Plain-text plan built from what we know (no LLM, so it is fast and predictable)."""
    memories = db.scalars(
        select(Memory)
        .where(Memory.scope == "lead", Memory.scope_id == lead.id, Memory.status == "active")
        .order_by(Memory.created_at)
    ).all()
    tasks = db.scalars(
        select(Task).where(Task.lead_id == lead.id, Task.status == "open").order_by(Task.created_at)
    ).all()

    name = lead.full_name or lead.email or "Patient"
    lines = [
        f"Treatment plan summary: {name}",
        f"Prepared on {date.today():%d %b %Y}",
        "",
        "CONTACT",
        f"Email: {lead.email or '-'}",
        f"Phone: {lead.phone or '-'}",
        f"Stage: {lead.status}",
    ]
    for key, value in (lead.profile or {}).items():
        lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")

    for kind, title in (("fact", "KEY FACTS"), ("preference", "PREFERENCES"), ("commitment", "COMMITMENTS"), ("episode", "HISTORY")):
        items = [m.content for m in memories if m.kind == kind]
        if items:
            lines += ["", title] + [f"- {item}" for item in items]
    if tasks:
        lines += ["", "OPEN FOLLOW-UPS"] + [f"- {t.title}" for t in tasks]
    lines += ["", "NOTES FOR THE DOCTOR", "", ""]
    return f"Treatment plan - {name}", "\n".join(lines)


def create_plan_doc(db: Session, org, lead: Lead) -> str:
    """Create the Google Doc and return its URL."""
    account = google_oauth.get_google_account(db, org.id, scope=google_oauth.DOCS_SCOPE)
    token = google_oauth.get_access_token(db, account)
    title, text = build_plan(db, lead)
    headers = {"Authorization": f"Bearer {token}"}

    with http.make_client() as client:
        created = http.request_with_retry(client, "POST", DOCS_API, json={"title": title}, headers=headers, throttle=_throttle)
        if created.status_code != 200:
            raise http.UpstreamError(f"Docs create returned {created.status_code}: {created.text[:100]}", created.status_code)
        doc_id = created.json()["documentId"]
        updated = http.request_with_retry(
            client, "POST", f"{DOCS_API}/{doc_id}:batchUpdate",
            json={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]},
            headers=headers, throttle=_throttle,
        )
        if updated.status_code != 200:
            raise http.UpstreamError(f"Docs update returned {updated.status_code}: {updated.text[:100]}", updated.status_code)
    return f"https://docs.google.com/document/d/{doc_id}/edit"
