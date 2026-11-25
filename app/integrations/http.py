"""Shared HTTP behaviour for every third-party call: retries, Retry-After, throttling."""
import random
import time

import httpx


class UpstreamError(Exception):
    """A third-party API failed (even after retries).

    permanent=True means retrying cannot help (bad credentials, bad request).
    """

    def __init__(self, message: str, status: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


def make_client() -> httpx.Client:
    # Tests replace this function to inject a fake transport.
    return httpx.Client(timeout=20)


class Throttle:
    """Space calls so we never exceed `rate` requests per second (client-side rate limit)."""

    def __init__(self, rate_per_second: float):
        self.min_interval = 1.0 / rate_per_second
        self._last = 0.0

    def wait(self) -> None:
        delay = self._last + self.min_interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return min(60.0, float(value)) if value else None
    except ValueError:
        return None


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429 or response.status_code >= 500:
        return True
    # Google sometimes signals quota problems as 403 with a rateLimitExceeded reason
    return response.status_code == 403 and "ratelimitexceeded" in response.text.lower()


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    throttle: Throttle | None = None,
    max_retries: int = 4,
    **kwargs,
) -> httpx.Response:
    """Send a request. Retry on 429/5xx/network errors, honouring Retry-After,
    otherwise exponential backoff with jitter. Other statuses are returned to the caller."""
    for attempt in range(max_retries + 1):
        if throttle:
            throttle.wait()
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == max_retries:
                raise UpstreamError(f"network error calling {url}: {exc}") from exc
            time.sleep(min(30, 2**attempt) + random.random())
            continue

        if not _is_rate_limited(response):
            return response
        if attempt == max_retries:
            raise UpstreamError(
                f"{response.status_code} from {url} after {max_retries} retries",
                status=response.status_code,
            )
        time.sleep(_retry_after(response) or min(30, 2**attempt) + random.random())
    raise AssertionError("unreachable")
