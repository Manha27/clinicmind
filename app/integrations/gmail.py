"""Gmail: incremental sync of emails that belong to known leads."""
import base64
import html
import re
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations import google_oauth, http
from app.jobs.queue import enqueue, enqueue_extraction
from app.models import IntegrationAccount, Job, Lead, Message, SyncCursor, Thread

API = "https://gmail.googleapis.com/gmail/v1/users/me"
RESOURCE = "gmail_history"
MAX_FULL_SYNC = 300  # cap on the first sync, so it stays quick
_throttle = http.Throttle(5.0)  # our own limit: 5 requests/second


class _HistoryExpired(Exception):
    """Gmail only keeps history for about a week; older cursors need a full resync."""


def _get(client, token: str, path: str, params: dict | None = None):
    return http.request_with_retry(
        client, "GET", API + path, params=params,
        headers={"Authorization": f"Bearer {token}"}, throttle=_throttle,
    )


# ---------- parsing ----------

def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _walk(part: dict, found: dict) -> None:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        found.setdefault(mime, []).append(_decode(data))
    for sub in part.get("parts") or []:
        _walk(sub, found)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n+", "\n\n", html.unescape(text))


def _strip_quoted(text: str) -> str:
    """Drop the quoted earlier conversation that mail clients append to replies."""
    head = re.split(r"\n\s*On .{1,200}?wrote:\s*\n", "\n" + text, maxsplit=1)[0]
    lines = [ln for ln in head.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines)


def extract_text(payload: dict) -> str:
    found: dict[str, list[str]] = {}
    _walk(payload, found)
    if found.get("text/plain"):
        text = "\n".join(found["text/plain"])
    elif found.get("text/html"):
        text = _strip_html("\n".join(found["text/html"]))
    else:
        text = ""
    return _strip_quoted(text).strip()[:4000]


def ingest_email(db: Session, org_id, account_email: str, msg: dict) -> Message | None:
    """Store one Gmail message against the matching lead. Returns None if it is not ours to store."""
    sender = parseaddr(_header(msg, "from"))[1].lower()
    recipients = [a.lower() for _, a in getaddresses([_header(msg, "to"), _header(msg, "cc")]) if a]
    if sender == account_email.lower():
        direction, candidates = "out", recipients
    else:
        direction, candidates = "in", [sender]
    if not candidates:
        return None

    lead = db.scalar(
        select(Lead).where(Lead.org_id == org_id, func.lower(Lead.email).in_(candidates)).limit(1)
    )
    if lead is None:  # we only keep emails from people who are already leads
        return None

    thread = db.scalar(
        select(Thread).where(Thread.channel == "email", Thread.external_thread_id == msg["threadId"])
    )
    if thread is None:
        thread = Thread(lead_id=lead.id, channel="email", external_thread_id=msg["threadId"])
        db.add(thread)
        db.flush()
    elif thread.lead_id != lead.id:
        return None

    if db.scalar(select(Message.id).where(Message.thread_id == thread.id, Message.external_id == msg["id"])):
        return None  # already synced (this is what makes re-syncing safe)

    body = extract_text(msg.get("payload", {}))
    subject = _header(msg, "subject")
    text = f"Subject: {subject}\n\n{body}" if subject else body
    if not text.strip():
        return None
    message = Message(
        thread_id=thread.id,
        lead_id=lead.id,
        direction=direction,
        body=text,
        sent_at=datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc),
        external_id=msg["id"],
    )
    db.add(message)
    db.flush()
    return message


# ---------- fetching ----------

def _full_ids(client, token: str) -> tuple[list[str], str]:
    profile = _get(client, token, "/profile")
    if profile.status_code != 200:
        raise http.UpstreamError(f"Gmail profile returned {profile.status_code}", profile.status_code)
    # Read the cursor BEFORE listing: mail arriving meanwhile is then picked up next time.
    cursor = str(profile.json()["historyId"])

    ids: list[str] = []
    page = None
    while len(ids) < MAX_FULL_SYNC:
        params = {"maxResults": 100, "q": f"newer_than:{settings.gmail_lookback_days}d"}
        if page:
            params["pageToken"] = page
        response = _get(client, token, "/messages", params)
        if response.status_code != 200:
            raise http.UpstreamError(f"Gmail list returned {response.status_code}", response.status_code)
        body = response.json()
        ids += [m["id"] for m in body.get("messages", [])]
        page = body.get("nextPageToken")
        if not page:
            break
    ids.reverse()  # Gmail lists newest first; we want oldest first
    return ids[:MAX_FULL_SYNC], cursor


def _incremental_ids(client, token: str, cursor: str) -> tuple[list[str], str]:
    ids: list[str] = []
    new_cursor = cursor
    page = None
    while True:
        params = {"startHistoryId": cursor, "historyTypes": "messageAdded", "maxResults": 100}
        if page:
            params["pageToken"] = page
        response = _get(client, token, "/history", params)
        if response.status_code == 404:
            raise _HistoryExpired
        if response.status_code != 200:
            raise http.UpstreamError(f"Gmail history returned {response.status_code}", response.status_code)
        body = response.json()
        for record in body.get("history", []):
            for added in record.get("messagesAdded", []):
                if added["message"]["id"] not in ids:
                    ids.append(added["message"]["id"])
        new_cursor = str(body.get("historyId", new_cursor))
        page = body.get("nextPageToken")
        if not page:
            return ids, new_cursor


# ---------- sync ----------

def _save_cursor(db: Session, account_id, value: str) -> None:
    row = db.get(SyncCursor, (account_id, RESOURCE))
    if row is None:
        row = SyncCursor(account_id=account_id, resource=RESOURCE)
        db.add(row)
    row.cursor = value
    row.last_synced_at = datetime.now(timezone.utc)
    db.commit()


def sync_account(db: Session, account: IntegrationAccount) -> dict:
    token = google_oauth.get_access_token(db, account)
    row = db.get(SyncCursor, (account.id, RESOURCE))
    cursor = row.cursor if row else None

    stored = 0
    lead_ids = set()
    try:
        with http.make_client() as client:
            if cursor:
                try:
                    ids, new_cursor = _incremental_ids(client, token, cursor)
                except _HistoryExpired:
                    ids, new_cursor = _full_ids(client, token)
            else:
                ids, new_cursor = _full_ids(client, token)

            for n, message_id in enumerate(ids, 1):
                response = _get(client, token, f"/messages/{message_id}", {"format": "full"})
                if response.status_code == 404:  # deleted since it was listed
                    continue
                if response.status_code != 200:
                    raise http.UpstreamError(f"Gmail get returned {response.status_code}", response.status_code)
                message = ingest_email(db, account.org_id, account.account_email, response.json())
                if message:
                    stored += 1
                    lead_ids.add(message.lead_id)
                if n % 25 == 0:
                    db.commit()
    except http.UpstreamError as exc:
        db.rollback()
        if exc.status == 401:  # token was rejected: force a refresh on the next attempt
            account.expires_at = None
            db.commit()
        raise

    db.commit()
    _save_cursor(db, account.id, new_cursor)
    for lead_id in lead_ids:
        enqueue_extraction(db, lead_id)
    db.commit()
    return {"emails_seen": len(ids), "messages_stored": stored, "leads_touched": len(lead_ids)}


def schedule_syncs(db: Session) -> None:
    """Called by the worker every ~30s: queue a sync for each connected account that is due."""
    if not settings.google_client_id:
        return
    now = datetime.now(timezone.utc)
    for account in db.scalars(
        select(IntegrationAccount).where(
            IntegrationAccount.provider == "google", IntegrationAccount.status == "active"
        )
    ):
        row = db.get(SyncCursor, (account.id, RESOURCE))
        due = row is None or row.last_synced_at is None or (
            (now - row.last_synced_at).total_seconds() >= settings.gmail_sync_interval_s
        )
        key = f"gmail_sync:{account.id}"
        busy = db.scalar(select(Job.id).where(Job.dedupe_key == key, Job.status.in_(("queued", "running"))))
        if due and not busy:
            enqueue(db, "gmail_sync", {"account_id": str(account.id)}, dedupe_key=key)
    db.commit()
