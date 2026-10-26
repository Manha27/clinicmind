from google import genai

from app.config import settings

client = genai.Client(api_key=settings.gemini_api_key)
for m in client.models.list():
    actions = m.supported_actions or []
    if "generateContent" in actions or "embedContent" in actions:
        print(m.name, actions)