import argparse
import hashlib
import hmac
import json
import time

import httpx

from app.config import settings

URL = "http://127.0.0.1:8000/webhooks/portal"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--event-id", default="evt_001")
    p.add_argument("--email", default="rahul@example.com")
    p.add_argument("--bad-signature", action="store_true", help="sign with the wrong secret")
    p.add_argument("--tamper", action="store_true", help="change the body after signing")
    p.add_argument("--old", action="store_true", help="use a 10-minute-old timestamp")
    args = p.parse_args()

    payload = {
        "event_id": args.event_id,
        "name": "Rahul Verma",
        "email": args.email,
        "phone": "+91 99999 11111",
        "treatment": "teeth whitening",
        "city": "Mohali",
        "message": "Hi, what is the cost of whitening? I can only visit in the evening.",
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()) - (600 if args.old else 0))

    secret = "wrong-secret" if args.bad_signature else settings.webhook_secret
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()

    if args.tamper:
        body = body.replace(b"whitening", b"surgery!!")

    response = httpx.post(
        URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": timestamp,
            "X-Signature": f"sha256={signature}",
        },
    )
    print(response.status_code, response.text)


if __name__ == "__main__":
    main()