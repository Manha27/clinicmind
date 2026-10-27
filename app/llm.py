import math
import time
import math
import random
import time
import math
import random
import time

import httpx
from google import genai
from google.genai import errors, types
from google import genai
from google.genai import errors, types
from google import genai
from google.genai import types
from sqlalchemy import exc

from app.config import settings
from app.db import SessionLocal
from app.models import LLMCall

_client = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set in .env")
        _client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=settings.llm_timeout_s * 1000),  # milliseconds
        )    
    return _client

class LLMUnavailable(Exception):
    """The AI provider is overloaded or rate-limited, even after retries."""


RETRY_DELAYS = [2, 6]  # seconds to wait before each retry


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, errors.ServerError):  # 500, 503...
        return True
    return isinstance(exc, errors.ClientError) and getattr(exc, "code", None) == 429


def call_with_retry(fn):
    """Run fn(); on 5xx/429 wait (with a little random jitter) and try again."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            if attempt == len(RETRY_DELAYS):
                raise LLMUnavailable(str(exc)[:300]) from exc
            time.sleep(RETRY_DELAYS[attempt] + random.random())

def estimate_cost(prompt_tokens: int | None, completion_tokens: int | None) -> float:
    return (
        (prompt_tokens or 0) * settings.price_in_per_m
        + (completion_tokens or 0) * settings.price_out_per_m
    ) / 1_000_000


def log_call(
    *,
    purpose: str,
    model: str,
    latency_ms: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    ok: bool = True,
    error: str | None = None,
    manifest: dict | None = None,
) -> None:
    # Own session, so the log survives even if the caller's transaction rolls back
    with SessionLocal() as db:
        db.add(
            LLMCall(
                purpose=purpose,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                cost_usd=estimate_cost(prompt_tokens, completion_tokens),
                ok=ok,
                error=error,
                context_manifest=manifest,
            )
        )
        db.commit()


def generate(
    prompt: str,
    *,
    purpose: str,
    system: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.2,
    manifest: dict | None = None,
) -> str:
    """Call the LLM and log tokens, latency and estimated cost."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json" if json_mode else None,
    )
    start = time.perf_counter()
    try:
         response = call_with_retry(
            lambda: get_client().models.generate_content(
                model=settings.llm_model, contents=prompt, config=config
            )
        )
    except Exception as exc:
        log_call(
            purpose=purpose,
            model=settings.llm_model,
            latency_ms=int((time.perf_counter() - start) * 1000),
            ok=False,
            error=str(exc)[:500],
            manifest=manifest,
        )
        raise

    usage = response.usage_metadata
    prompt_tokens = getattr(usage, "prompt_token_count", None) or 0
    # "thinking" tokens are billed like output tokens, so count them
    completion_tokens = (getattr(usage, "candidates_token_count", None) or 0) + (
        getattr(usage, "thoughts_token_count", None) or 0
    )
    log_call(
        purpose=purpose,
        model=settings.llm_model,
        latency_ms=int((time.perf_counter() - start) * 1000),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        manifest=manifest,
    )
    return response.text or ""


def embed(texts: list[str], *, task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Return one normalised vector per text. Use task='RETRIEVAL_QUERY' for search queries."""
    start = time.perf_counter()
    try:
        response = call_with_retry(
            lambda: get_client().models.embed_content(
                model=settings.embed_model,
                contents=texts,
                config=types.EmbedContentConfig(
                    task_type=task, output_dimensionality=settings.embed_dim
                ),
            )
        )
    except Exception as exc:
        log_call(
            purpose="embed",
            model=settings.embed_model,
            latency_ms=int((time.perf_counter() - start) * 1000),
            ok=False,
            error=str(exc)[:500],
        )
        raise

    log_call(
        purpose="embed",
        model=settings.embed_model,
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    vectors = []
    for e in response.embeddings:
        v = list(e.values)
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        vectors.append([x / norm for x in v])  # smaller dimensions need re-normalising
    return vectors