from app.config import settings
from app.integrations import http

# Slack incoming webhooks allow roughly one message per second
_throttle = http.Throttle(1.0)


def send_message(text: str) -> None:
    if not settings.slack_webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL is not set in .env")
    with http.make_client() as client:
        response = http.request_with_retry(
            client, "POST", settings.slack_webhook_url, json={"text": text}, throttle=_throttle
        )
    if response.status_code != 200:
        # 400/403/404 mean the webhook URL is wrong or revoked: retrying will not help
        raise http.UpstreamError(
            f"Slack returned {response.status_code}: {response.text[:100]}",
            status=response.status_code,
            permanent=response.status_code in (400, 403, 404),
        )
