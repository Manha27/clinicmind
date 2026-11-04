"""Time a tiny request on several Gemini models.

    python -m scripts.bench_models
    python -m scripts.bench_models gemini-2.5-flash gemini-3.5-flash-lite

Single attempt per call (no retries), so you see Google's real behaviour right now.
"""
import sys
import time

from google import genai
from google.genai import types

from app.config import settings

DEFAULT = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
MODELS = sys.argv[1:] or DEFAULT

client = genai.Client(
    api_key=settings.gemini_api_key,
    http_options=types.HttpOptions(timeout=settings.llm_timeout_s * 1000),
)

for name in MODELS:
    for attempt in (1, 2, 3):
        start = time.perf_counter()
        try:
            client.models.generate_content(model=name, contents="Reply with the single word: ok")
            result = "OK"
        except Exception as exc:  # noqa: BLE001
            result = f"FAILED {getattr(exc, 'code', '')} {str(exc)[:70]}"
        print(f"{name:26} try {attempt}: {time.perf_counter() - start:5.1f}s  {result}")
