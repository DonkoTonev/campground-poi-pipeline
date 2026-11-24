"""Token counts for cost accounting.

`estimate` is a local approximation (UTF-8 bytes / 4) used when no API key is configured. It
is labelled as an estimate wherever it is reported; the extraction stage calibrates it
against the API's free `count_tokens` endpoint on a sample and stores the correction factor.
"""

from __future__ import annotations

import math

BYTES_PER_TOKEN = 4.0
METHOD = "estimate:utf8_bytes/4"


def estimate(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / BYTES_PER_TOKEN) if text else 0
