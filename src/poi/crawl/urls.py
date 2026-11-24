"""Seed and link handling. Seeds are untrusted data: canonicalize, screen, and scope them.

Pure functions (plus a DNS lookup for SSRF screening) so every rule is unit-testable.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

from poi.normalize.contact import TRACKING_PARAMS, canonical_url, is_social

# Offline public-suffix list (bundled snapshot): no network access at import or run time.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())


def registrable_domain(url: str) -> str:
    ext = _EXTRACT(url)
    return ext.top_domain_under_public_suffix or (urlsplit(url).hostname or "")


def _matches(host_or_url: str, entries: list[str]) -> str | None:
    """Entries are domains ("hipcamp.com") or domain/path prefixes
    ("koa.com/campgrounds/search")."""
    parts = urlsplit(host_or_url if "://" in host_or_url else f"http://{host_or_url}")
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path
    for entry in entries:
        domain, _, prefix = entry.partition("/")
        if (host == domain or host.endswith("." + domain)) and path.startswith("/" + prefix):
            return entry
    return None


@dataclass(frozen=True)
class SeedDecision:
    url: str | None  # canonical URL to crawl, or None when skipped
    reason: str | None  # why skipped
    booking_platform: str | None = None


def resolves_to_public_ip(host: str) -> bool:
    """SSRF guard: every address the host resolves to must be public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def screen_seed(raw: str, cfg: dict[str, Any], allow_private: bool = False) -> SeedDecision:
    """`allow_private` exists for tests against a local server only."""
    url = canonical_url(raw)
    if url is None:
        return SeedDecision(None, "invalid_or_non_http_url")
    if is_social(url):
        return SeedDecision(None, "social_media")
    if platform := booking_platform(url, cfg):
        return SeedDecision(None, "booking_engine", platform)
    skip = cfg["skip_domains"]
    for reason in ("api_sources", "aggregators_and_reviews", "not_official"):
        if _matches(url, skip[reason]):
            return SeedDecision(None, reason)
    if allow_private:
        return SeedDecision(url, None)
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return SeedDecision(None, "ip_literal_host")
    except ValueError:
        pass
    if not resolves_to_public_ip(host):
        return SeedDecision(None, "private_or_unresolvable_host")
    return SeedDecision(url, None)


def booking_platform(url: str, cfg: dict[str, Any]) -> str | None:
    platforms: dict[str, str] = cfg["booking_platforms"]
    entry = _matches(url, list(platforms))
    return platforms[entry] if entry else None


@dataclass(frozen=True)
class Scope:
    """Where a site's crawl may go: same registrable domain, optionally a path subtree and
    required query parameters (for multi-location hosts with generic paths)."""

    domain: str
    path_prefix: str = "/"
    query: tuple[tuple[str, str], ...] = ()

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        if registrable_domain(url) != self.domain:
            return False
        if not (parts.path + "/").startswith(self.path_prefix.rstrip("/") + "/"):
            return False
        params = dict(parse_qsl(parts.query))
        return all(params.get(k) == v for k, v in self.query)


def scope_for(seed: str, cfg: dict[str, Any]) -> Scope:
    domain = registrable_domain(seed)
    parts = urlsplit(seed)
    if not _matches(seed, cfg["scope"]["multi_location_hosts"]) or parts.path in ("", "/"):
        return Scope(domain)
    query = tuple(sorted(parse_qsl(parts.query)))
    if query:
        # A generic path (".../recarea?recid=123") identifies the location by its query.
        return Scope(domain, parts.path, query)
    return Scope(domain, parts.path)


@lru_cache(maxsize=1)
def _lower(words: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(w.lower() for w in words)


def link_relevance(url: str, anchor_text: str, cfg: dict[str, Any]) -> float | None:
    """Score a link for the per-site page budget; None means "never follow"."""
    scope = cfg["scope"]
    parts = urlsplit(url)
    if any(k.lower() in scope["exclude_query_params"] for k, _ in parse_qsl(parts.query)):
        return None  # calendars and other infinite spaces
    haystack = f"{parts.path} {anchor_text}".lower()
    if any(w in haystack for w in _lower(tuple(scope["exclude_keywords"]))):
        return None
    if parts.path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp4", ".ics")):
        return None
    hits = sum(w in haystack for w in _lower(tuple(scope["include_keywords"])))
    depth_penalty = 0.1 * parts.path.count("/")
    return hits - depth_penalty


def request_url(raw: str) -> str | None:
    """The URL to actually fetch: tracking parameters and fragment removed, host and scheme
    kept as written. `canonical_url` (no www, no trailing slash) is the page's *identity*
    for de-duplication and scope; requesting it can just bounce off a www redirect."""
    parts = urlsplit(raw.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not TRACKING_PARAMS.match(k)
        ]
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))
