from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
