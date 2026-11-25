import math
import random
import time
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import errors, types

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
            http_options=types.HttpOptions(timeout=settings.llm_timeout_s * 1000),
        )
    return _client


class LLMUnavailable(Exception):
    """The AI provider is overloaded or rate-limited, even after retries."""


RETRY_DELAYS = [2, 6]  # seconds to wait before each retry
FAST_FAIL_DELAYS = [2]  # when a fallback model exists, give up on the main model sooner


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, errors.ServerError):
        return True
    return isinstance(exc, errors.ClientError) and getattr(exc, "code", None) == 429


def call_with_retry(fn, delays=None):
    delays = RETRY_DELAYS if delays is None else delays
    for attempt in range(len(delays) + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            if attempt == len(delays):
                raise LLMUnavailable(str(exc)[:300]) from exc
            time.sleep(delays[attempt] + random.random())


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


@dataclass
class ModelResult:
    """What came back from the model, plus what it cost."""

    response: object  # the raw SDK response (the agent needs its function calls)
    model: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def text(self) -> str:
        return self.response.text or ""


def call_model(contents, config, *, purpose: str, manifest: dict | None = None) -> ModelResult:
    """Call the main model (with retries). If it stays unavailable, try the fallback model.
    Every attempt is logged in llm_calls."""
    models = [settings.llm_model]
    if settings.llm_fallback_model and settings.llm_fallback_model != settings.llm_model:
        models.append(settings.llm_fallback_model)

    last_error: LLMUnavailable | None = None
    for i, model in enumerate(models):
        is_last = i == len(models) - 1
        start = time.perf_counter()
        try:
            response = call_with_retry(
                lambda: get_client().models.generate_content(
                    model=model, contents=contents, config=config
                ),
                RETRY_DELAYS if is_last else FAST_FAIL_DELAYS,
            )
        except LLMUnavailable as exc:
            log_call(
                purpose=purpose,
                model=model,
                latency_ms=int((time.perf_counter() - start) * 1000),
                ok=False,
                error=str(exc)[:500],
                manifest=manifest,
            )
            last_error = exc
            continue  # try the next model, if any
        except Exception as exc:
            log_call(
                purpose=purpose,
                model=model,
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
        latency_ms = int((time.perf_counter() - start) * 1000)
        log_call(
            purpose=purpose,
            model=model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            manifest=manifest,
        )
        return ModelResult(response, model, latency_ms, prompt_tokens, completion_tokens)

    raise last_error  # every model was unavailable


def generate_full(
    prompt: str,
    *,
    purpose: str,
    system: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.2,
    manifest: dict | None = None,
) -> ModelResult:
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json" if json_mode else None,
    )
    return call_model(prompt, config, purpose=purpose, manifest=manifest)


def generate(
    prompt: str,
    *,
    purpose: str,
    system: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.2,
    manifest: dict | None = None,
) -> str:
    return generate_full(
        prompt, purpose=purpose, system=system, json_mode=json_mode,
        temperature=temperature, manifest=manifest,
    ).text


def embed(texts: list[str], *, task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
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
        vectors.append([x / norm for x in v])
    return vectors
