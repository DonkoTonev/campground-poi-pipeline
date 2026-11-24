"""Polite JSON-API HTTP: honest User-Agent, timeouts, retries that honour `Retry-After`."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from poi.log import get_logger
from poi.settings import Settings

log = get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 300.0


class RetryableStatusError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code} from {response.request.url.host}")
        self.response = response
        self.retry_after = parse_retry_after(response.headers.get("retry-after"))


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP date. Returns seconds, or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


_backoff = wait_exponential_jitter(initial=1, max=60)


def _wait(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, RetryableStatusError) and exc.retry_after is not None:
        return min(exc.retry_after, MAX_RETRY_AFTER_S)
    return float(_backoff(state))


def make_client(settings: Settings, timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=True,
    )


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    max_attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """Send one request; retry transport errors and 429/5xx with backoff or `Retry-After`.

    Non-retryable errors (4xx other than 429) raise `httpx.HTTPStatusError` immediately.
    """
    retrying = Retrying(
        retry=retry_if_exception_type((RetryableStatusError, httpx.TransportError)),
        wait=_wait,
        stop=stop_after_attempt(max_attempts),
        sleep=sleep,
        reraise=True,
        before_sleep=lambda s: log.warning(
            "http.retry",
            url=url,
            attempt=s.attempt_number,
            error=str(s.outcome.exception()) if s.outcome else None,
            wait_s=round(s.upcoming_sleep, 1),
        ),
    )
    for attempt in retrying:
        with attempt:
            resp = client.request(method, url, params=params, data=data, headers=headers)
            if resp.status_code in RETRY_STATUSES:
                raise RetryableStatusError(resp)
            resp.raise_for_status()
            return resp
    raise AssertionError("unreachable")  # pragma: no cover
