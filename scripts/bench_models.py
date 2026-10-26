import time

from google import genai

from app.config import settings

MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]

client = genai.Client(api_key=settings.gemini_api_key)

for name in MODELS:
    for _ in range(2):
        start = time.perf_counter()
        try:
            r = client.models.generate_content(
                model=name, contents="Reply with the single word: ok"
            )
            ms = int((time.perf_counter() - start) * 1000)
            u = r.usage_metadata
            print(
                f"{name:28} {ms:>6} ms  prompt={u.prompt_token_count} "
                f"out={u.candidates_token_count} thoughts={u.thoughts_token_count}"
            )
        except Exception as exc:
            print(f"{name:28} FAILED {str(exc)[:100]}")