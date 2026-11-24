from datetime import UTC, datetime

import httpx
import pytest
import respx

from poi import http

URL = "https://api.example.org/x"


def test_parse_retry_after_seconds_and_date() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert http.parse_retry_after("7") == 7.0
    assert http.parse_retry_after("Thu, 01 Jan 2026 12:00:30 GMT", now) == 30.0
    assert http.parse_retry_after("garbage") is None
    assert http.parse_retry_after(None) is None


@respx.mock
def test_429_waits_for_retry_after_then_succeeds() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "12"}),
            httpx.Response(200, json={}),
        ]
    )
    slept: list[float] = []
    with httpx.Client() as client:
        resp = http.request(client, "GET", URL, sleep=slept.append)
    assert resp.status_code == 200 and route.call_count == 2
    assert slept == [12.0]


@respx.mock
def test_retry_after_is_capped() -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": "99999"}), httpx.Response(200)]
    )
    slept: list[float] = []
    with httpx.Client() as client:
        http.request(client, "GET", URL, sleep=slept.append)
    assert slept == [http.MAX_RETRY_AFTER_S]


@respx.mock
def test_5xx_without_retry_after_uses_backoff_and_gives_up() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(502))
    slept: list[float] = []
    with httpx.Client() as client, pytest.raises(http.RetryableStatusError):
        http.request(client, "GET", URL, max_attempts=3, sleep=slept.append)
    assert route.call_count == 3 and len(slept) == 2 and all(s > 0 for s in slept)


@respx.mock
def test_4xx_is_not_retried() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(401))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        http.request(client, "GET", URL, sleep=lambda _: None)
    assert route.call_count == 1
