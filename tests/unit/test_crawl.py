"""Crawler rules: seed screening, scope, link ranking, page classification, Retry-After."""

import asyncio
from typing import Any

import pytest
from scrapy import Request
from scrapy.http import HtmlResponse

from poi.crawl.detect import analyse
from poi.crawl.middleware import RetryAfterMiddleware
from poi.crawl.urls import (
    Scope,
    link_relevance,
    registrable_domain,
    request_url,
    scope_for,
    screen_seed,
)
from poi.settings import load_yaml

CFG = load_yaml("crawl.yaml")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("ftp://example.com/camp", "invalid_or_non_http_url"),
        ("javascript:alert(1)", "invalid_or_non_http_url"),
        ("https://www.facebook.com/somecampground", "social_media"),
        ("https://www.recreation.gov/camping/campgrounds/232447", "booking_engine"),
        ("https://www.nps.gov/yose/planyourvisit/camping.htm", "api_sources"),
        ("https://www.hipcamp.com/en-US/land/california-x", "aggregators_and_reviews"),
        ("http://127.0.0.1/admin", "ip_literal_host"),
        ("http://192.168.1.10/", "ip_literal_host"),
        ("http://localhost:8080/", "private_or_unresolvable_host"),
        ("http://metadata.google.internal/", "private_or_unresolvable_host"),  # cloud metadata
    ],
)
def test_untrusted_seeds_are_screened(raw: str, reason: str) -> None:
    decision = screen_seed(raw, CFG)
    assert decision.url is None and decision.reason == reason


def test_booking_engine_seed_records_the_platform() -> None:
    assert screen_seed("https://www.campspot.com/book/somepark", CFG).booking_platform == "campspot"


def test_seed_is_canonicalized() -> None:
    d = screen_seed("www.Example.com/Camp/?utm_source=osm&fbclid=1", CFG, allow_private=True)
    assert d.url == "http://example.com/Camp"


def test_scope_same_registrable_domain() -> None:
    scope = scope_for("https://www.monovillage.com/", CFG)
    assert scope == Scope("monovillage.com")
    assert scope.allows("https://monovillage.com/rates")
    assert scope.allows("https://shop.monovillage.com/rates")
    assert not scope.allows("https://monovillage.net/rates")


def test_multi_location_host_stays_in_path_subtree_or_query() -> None:
    deep = scope_for("https://fs.usda.gov/r05/sierra/recreation/dry-gulch-campground", CFG)
    assert deep.allows("https://fs.usda.gov/r05/sierra/recreation/dry-gulch-campground/fees")
    assert not deep.allows("https://fs.usda.gov/r05/sierra/recreation/other-campground")
    generic = scope_for("https://fs.usda.gov/recarea/htnf/recarea?recid=65208", CFG)
    assert generic.allows("https://fs.usda.gov/recarea/htnf/recarea?recid=65208&tab=fees")
    assert not generic.allows("https://fs.usda.gov/recarea/htnf/recarea?recid=65210")
    assert registrable_domain("https://www.fs.usda.gov/x") == "usda.gov"


def test_link_ranking_prefers_relevant_pages_and_excludes_traps() -> None:
    rates = link_relevance("https://x.com/rates-and-fees", "Rates & Fees", CFG)
    about = link_relevance("https://x.com/our-story", "Our story", CFG)
    assert rates is not None and about is not None and rates > about
    for url, text in [
        ("https://x.com/blog/summer", "Blog"),
        ("https://x.com/events/calendar", "Events"),
        ("https://x.com/sites?date=2026-07-04", "Availability"),
        ("https://x.com/cart", "Cart"),
        ("https://x.com/photo.jpg", "Photo"),
    ]:
        assert link_relevance(url, text, CFG) is None


@pytest.mark.parametrize(
    ("html", "status", "flag"),
    [
        ("<title>Attention Required! | Cloudflare</title><body>captcha</body>", 403, "blocked"),
        ("<title>Service unavailable</title><body>The request is blocked.</body>", 403, "blocked"),
        ("<body>This domain is for sale! Buy this domain today.</body>", 200, "parked"),
        ("<body><h1>Oops! Page not found</h1><p>Try the home page.</p></body>", 200, "soft_404"),
        ("<head><title>App</title></head><body>" + "<script></script>" * 5 + "<div id=root></div></body>",
         200, "needs_render"),
    ],
)  # fmt: skip
def test_page_classification(html: str, status: int, flag: str) -> None:
    assert flag in analyse(html, status, 400).flags


def test_content_page_mentioning_not_found_is_not_a_soft_404() -> None:
    text = "Upper Pines has 238 sites. " * 80 + "Pets not found in wilderness areas."
    flags = analyse(f"<body><p>{text}</p></body>", 200, 400).flags
    assert "soft_404" not in flags and "needs_render" not in flags


def test_embedded_json_is_detected_and_prevents_render() -> None:
    html = ('<head><script type="application/ld+json">{"@type":"Campground"}</script></head>'
            "<body>" + "<script></script>" * 5 + "</body>")  # fmt: skip
    flags = analyse(html, 200, 400).flags
    assert "has_jsonld" in flags and "needs_render" not in flags


# --- Retry-After middleware -------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _response(status: int, headers: dict[str, str] | None = None) -> HtmlResponse:
    req = Request("https://x.com/rates")
    return HtmlResponse(req.url, status=status, headers=headers or {}, body=b"", request=req)


def test_retry_after_waits_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("poi.crawl.middleware.asyncio.sleep", fake_sleep)
    mw = RetryAfterMiddleware(max_attempts=3, max_wait_s=60)
    resp = _response(429, {"Retry-After": "7"})
    out = _run(mw.process_response(resp.request, resp, None))  # type: ignore[arg-type]
    assert isinstance(out, Request) and out.meta["retry_after_attempt"] == 1 and slept == [7.0]


def test_backoff_without_header_and_give_up_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("poi.crawl.middleware.asyncio.sleep", fake_sleep)
    mw = RetryAfterMiddleware(max_attempts=2, max_wait_s=60, base_s=2)
    resp = _response(503)
    resp.request.meta["retry_after_attempt"] = 1
    out = _run(mw.process_response(resp.request, resp, None))  # type: ignore[arg-type]
    assert isinstance(out, Request) and slept == [4.0]  # 2 * 2^1
    too_long = _response(429, {"Retry-After": "3600"})
    assert _run(mw.process_response(too_long.request, too_long, None)) is too_long  # type: ignore[arg-type]
    ok = _response(200)
    assert _run(mw.process_response(ok.request, ok, None)) is ok  # type: ignore[arg-type]


def test_request_url_keeps_the_real_host_but_drops_tracking() -> None:
    raw = "HTTPS://WWW.Example.com/Camp/?utm_source=x&page=2#top"
    assert request_url(raw) == "https://www.example.com/Camp/?page=2"
    assert request_url("mailto:x@example.com") is None
