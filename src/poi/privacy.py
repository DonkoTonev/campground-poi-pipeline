"""Scrub contact data from anything that will be committed (fixtures, demo snapshot).

Phones become fictional 555-01xx numbers and emails become example.com addresses. The mapping
is deterministic (hash-based), so the same real value always maps to the same placeholder and
cross-source equality, which entity resolution relies on, survives scrubbing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Separated NANP numbers. The lookarounds stop matches inside decimals such as coordinates
# ("37.7357410171" must survive), which a plain \b boundary would not.
# Separators seen in real feeds include "-", ".", " " and "/" (NPS: "209/372-...").
PHONE = re.compile(
    r"(?<![\d.])(?:\+?1[-. /]?)?\(?[2-9]\d{2}\)?[-. /][2-9]\d{2}[-. /]\d{4}(?![\d.])"
    r"|(?<![\d.])\([2-9]\d{2}\)\s?[2-9]\d{2}[-. /]?\d{4}(?![\d.])"
)
PHONE_KEY = re.compile(r"phone|fax|tel", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SAFE_EMAIL_DOMAINS = ("example.com", "example.org")


def _bucket(value: str, size: int) -> int:
    digest = hashlib.sha256(re.sub(r"\D", "", value).encode() or value.encode()).hexdigest()
    return int(digest, 16) % size


def fake_phone(real: str) -> str:
    return f"555-01{_bucket(real, 100):02d}"


def fake_email(real: str) -> str:
    if real.lower().endswith(SAFE_EMAIL_DOMAINS):
        return real
    return f"contact{_bucket(real.lower(), 10_000):04d}@example.com"


def scrub_text(text: str) -> str:
    text = EMAIL.sub(lambda m: fake_email(m.group(0)), text)
    return PHONE.sub(lambda m: fake_phone(m.group(0)), text)


def scrub(obj: Any, key: str = "") -> Any:
    """Recursively scrub every string in a JSON-like structure (keys are left alone)."""
    if isinstance(obj, str):
        if PHONE_KEY.search(key) and len(re.sub(r"\D", "", obj)) in (10, 11):
            return fake_phone(obj)  # any formatting, including bare digits
        return scrub_text(obj)
    if isinstance(obj, list):
        return [scrub(v, key) for v in obj]
    if isinstance(obj, dict):
        return {k: scrub(v, str(k)) for k, v in obj.items()}
    return obj
