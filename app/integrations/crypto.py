"""Encrypt OAuth tokens before they touch the database (Fernet = AES-128-CBC + HMAC)."""
from cryptography.fernet import Fernet

from app.config import settings

KEY_HELP = (
    "TOKEN_ENCRYPTION_KEY is not set in .env. Generate one with:\n"
    '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)


def _fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise RuntimeError(KEY_HELP)
    return Fernet(settings.token_encryption_key.encode())


def encrypt(text: str) -> bytes:
    return _fernet().encrypt(text.encode())


def decrypt(blob: bytes) -> str:
    return _fernet().decrypt(bytes(blob)).decode()
