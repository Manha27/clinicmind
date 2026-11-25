"""Google OAuth 2.0 (authorization-code flow with PKCE), token storage and refresh."""
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations import http
from app.integrations.crypto import decrypt, encrypt
from app.models import IntegrationAccount, Org, OAuthState

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/documents",
]
DOCS_SCOPE = SCOPES[1]
STATE_MAX_AGE = timedelta(minutes=10)


class NotConnected(Exception):
    """No usable Google account is connected for this organisation."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_pkce() -> tuple[str, str]:
    """PKCE: a random secret (verifier) and its SHA-256 hash (challenge).
    We send only the hash first; the secret proves later that the same client finishes the login."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def require_configured() -> None:
    if not (settings.google_client_id and settings.google_client_secret):
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set in .env")


def start_login(db: Session, org: Org) -> str:
    """Create a one-time state, remember the PKCE verifier, return the Google login URL."""
    require_configured()
    db.execute(delete(OAuthState).where(OAuthState.created_at < _now() - STATE_MAX_AGE))
    state = secrets.token_urlsafe(32)  # protects against login CSRF
    verifier, challenge = new_pkce()
    db.add(OAuthState(state=state, code_verifier=verifier, org_id=org.id))
    db.commit()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",  # we need a refresh token for background sync
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _token_request(data: dict) -> dict:
    with http.make_client() as client:
        response = http.request_with_retry(client, "POST", TOKEN_URL, data=data)
    if response.status_code != 200:
        try:
            error = response.json().get("error", "")
        except ValueError:
            error = ""
        raise http.UpstreamError(
            f"Google token endpoint returned {response.status_code} {error}".strip(),
            status=response.status_code,
            permanent=error in {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_request"},
        )
    return response.json()


def _profile_email(access_token: str) -> str:
    with http.make_client() as client:
        response = http.request_with_retry(
            client, "GET", PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
    if response.status_code != 200:
        raise http.UpstreamError(f"Gmail profile returned {response.status_code}", response.status_code)
    return response.json()["emailAddress"].lower()


def complete_login(db: Session, state: str, code: str) -> IntegrationAccount:
    """Handle Google's redirect back to us: check state, swap the code for tokens, store them."""
    row = db.get(OAuthState, state)
    if row is None or _now() - row.created_at > STATE_MAX_AGE:
        raise ValueError("Invalid or expired login attempt. Start again from /integrations/google/connect")
    verifier, org_id = row.code_verifier, row.org_id
    db.delete(row)  # single use
    db.commit()

    tokens = _token_request(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    )
    email = _profile_email(tokens["access_token"])

    account = db.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.org_id == org_id,
            IntegrationAccount.provider == "google",
            IntegrationAccount.account_email == email,
        )
    )
    if account is None:
        account = IntegrationAccount(org_id=org_id, provider="google", account_email=email)
        db.add(account)
    account.access_token_enc = encrypt(tokens["access_token"])
    account.expires_at = _now() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    account.scopes = tokens.get("scope", " ".join(SCOPES)).split()
    account.status = "active"
    if tokens.get("refresh_token"):
        account.refresh_token_enc = encrypt(tokens["refresh_token"])
    elif account.refresh_token_enc is None:
        raise ValueError(
            "Google did not return a refresh token. Remove this app at "
            "myaccount.google.com/permissions and connect again."
        )
    db.commit()
    return account


def get_access_token(db: Session, account: IntegrationAccount) -> str:
    """Return a valid access token, refreshing it (and saving the new one) when it is about to expire."""
    if account.status != "active":
        raise http.UpstreamError("Google account needs to be reconnected", permanent=True)
    if (
        account.access_token_enc
        and account.expires_at
        and account.expires_at - timedelta(seconds=60) > _now()
    ):
        return decrypt(account.access_token_enc)
    if account.refresh_token_enc is None:
        account.status = "needs_reauth"
        db.commit()
        raise http.UpstreamError("No refresh token stored", permanent=True)

    try:
        tokens = _token_request(
            {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": decrypt(account.refresh_token_enc),
                "grant_type": "refresh_token",
            }
        )
    except http.UpstreamError as exc:
        if exc.permanent:  # token revoked or expired for good: the user must log in again
            account.status = "needs_reauth"
            db.commit()
        raise
    account.access_token_enc = encrypt(tokens["access_token"])
    account.expires_at = _now() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    if tokens.get("refresh_token"):
        account.refresh_token_enc = encrypt(tokens["refresh_token"])
    db.commit()
    return tokens["access_token"]


def get_google_account(db: Session, org_id, *, scope: str | None = None) -> IntegrationAccount:
    stmt = select(IntegrationAccount).where(
        IntegrationAccount.org_id == org_id,
        IntegrationAccount.provider == "google",
        IntegrationAccount.status == "active",
    )
    for account in db.scalars(stmt):
        if scope is None or scope in (account.scopes or []):
            return account
    raise NotConnected("No connected Google account. Open /integrations/google/connect in your browser.")
