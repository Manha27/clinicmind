"""Send a signed fake WhatsApp message to your running API (like Meta's servers would).

    python -m scripts.send_whatsapp
    python -m scripts.send_whatsapp --from 919876543210 --text "Can I book for Friday?" --name "Asha Rao"

Needs WHATSAPP_APP_SECRET in .env. Re-sending the same --id shows that duplicates are ignored.
"""
import argparse
import hashlib
import hmac
import json
import time
import uuid

import httpx

from app.config import settings

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/whatsapp")
parser.add_argument("--from", dest="sender", default="919876543210")
parser.add_argument("--name", default="Asha Rao")
parser.add_argument("--text", default="Hi, do you do dental implants? My budget is about 70k.")
parser.add_argument("--id", default=f"wamid.{uuid.uuid4().hex[:12]}")
args = parser.parse_args()

if not settings.whatsapp_app_secret:
    raise SystemExit("Set WHATSAPP_APP_SECRET in .env first (any random string works for local testing).")

payload = {"entry": [{"changes": [{"value": {
    "contacts": [{"wa_id": args.sender, "profile": {"name": args.name}}],
    "messages": [{"id": args.id, "from": args.sender, "timestamp": str(int(time.time())), "type": "text", "text": {"body": args.text}}],
}}]}]}
body = json.dumps(payload).encode()
signature = hmac.new(settings.whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()

response = httpx.post(args.url, content=body, headers={"X-Hub-Signature-256": f"sha256={signature}", "Content-Type": "application/json"})
print(response.status_code, response.text)
