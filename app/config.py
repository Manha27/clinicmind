from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent  # the project folder, wherever Python is started from


class Settings(BaseSettings):
    database_url: str
    webhook_secret: str

    # LLM (defaults keep tests and CI working without a key)
    gemini_api_key: str = ""
    llm_model: str = "gemini-3.1-flash-lite"
    embed_model: str = "gemini-embedding-001"
    embed_dim: int = 768
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    llm_timeout_s: int = 30
    llm_fallback_model: str = ""  # tried if the main model stays unavailable

    # Background jobs
    job_lock_timeout_s: int = 600  # a "running" job older than this is assumed crashed

    # Slack (incoming webhook URL from api.slack.com/apps -> Incoming Webhooks)
    slack_webhook_url: str = ""

    # Google (Gmail + Docs) OAuth: console.cloud.google.com -> Credentials -> OAuth client (Web)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/integrations/google/callback"
    token_encryption_key: str = ""  # Fernet key, see README (encrypts OAuth tokens at rest)
    gmail_sync_interval_s: int = 300
    gmail_lookback_days: int = 30

    # WhatsApp Cloud API (Meta) webhook
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""

    # Absolute path: an MCP client (e.g. Claude Desktop) starts the server from another folder.
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")


settings = Settings()
