"""Contact data: phones to E.164, canonical URLs, lower-cased validated emails."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|fbclid|gclid|msclkid|mc_[a-z]+|ref)$", re.IGNORECASE)
SOCIAL_DOMAINS = (
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "linkedin.com",
    "pinterest.com",
    "yelp.com",
    "tripadvisor.com",
)
MULTI_PHONE_SEP = re.compile(r"\s*(?:;|,|\s+or\s+|\s+/\s+)\s*", re.IGNORECASE)
PLACEHOLDERS = frozenset({"", "n/a", "na", "none", "null", "tbd", "-", "unknown"})
NON_WEB_SCHEME = re.compile(r"^(mailto|tel|javascript|data|file|ftp):", re.IGNORECASE)
EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")


@dataclass(frozen=True)
class Phone:
    e164: str
    extension: str | None
    toll_free: bool


def is_placeholder(raw: str | None) -> bool:
    """ "N/A", "none" and friends mean "no value", not "invalid value"."""
    return raw is None or raw.strip().lower() in PLACEHOLDERS


def normalize_phone(raw: str | None) -> Phone | None:
    """Parse a US/CA number (first one if several are listed). None if invalid or absent."""
    if raw is None or is_placeholder(raw):
        return None
    # Several numbers in one field: "x OR y", "x; y", "x, y", "x / y". A bare slash is a
    # separator inside one number in NPS data ("209/372-...").
    first = MULTI_PHONE_SEP.split(raw.strip())[0]
    for region in ("US", "CA"):
        try:
            num = phonenumbers.parse(first, region)
        except NumberParseException:
            continue
        if phonenumbers.is_valid_number(num):
            return Phone(
                e164=phonenumbers.format_number(num, PhoneNumberFormat.E164),
                extension=num.extension or None,
                toll_free=phonenumbers.number_type(num) == PhoneNumberType.TOLL_FREE,
            )
    return None


def registrable_host(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def canonical_url(raw: str | None) -> str | None:
    """Canonical http(s) URL: lower-case host without www, no tracking params, no fragment,
    no trailing slash. Returns None for empty input or non-http(s) schemes."""
    if not raw:
        return None
    text = raw.strip()
    if NON_WEB_SCHEME.match(text):
        return None
    if "://" not in text:
        text = "http://" + text  # "www.example.com/camp" as written in many feeds
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = registrable_host(parts.hostname)
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not TRACKING_PARAMS.match(k)
        ]
    )
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host + port, path, query, ""))


def is_social(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS)


def normalize_email(raw: str | None) -> str | None:
    if raw is None or is_placeholder(raw):
        return None
    email = raw.strip().lower().removeprefix("mailto:")
    return email if EMAIL.match(email) else None
