"""RIDB facility descriptions as an extraction input (provenance `ridb_description`).

The Forest Service blocks our crawler, but publishes its campground descriptions through the
RIDB API we already ingest. Same publisher, official channel: the description HTML (plus fee
text and stay limit) becomes a small document, reduced like a web page.
"""

from __future__ import annotations

from typing import Any

from markdownify import markdownify

from poi.reduce.html import collapse

FIELDS = (
    ("FacilityDescription", "Description"),
    ("FacilityUseFeeDescription", "Fees"),
    ("StayLimit", "Stay limit"),
)


def document(payload: dict[str, Any]) -> str | None:
    sections = []
    for key, title in FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            body = collapse(markdownify(value, heading_style="ATX", strip=["img", "a"]))
            sections.append(f"## {title}\n\n{body}")
    if not sections:
        return None
    name = payload.get("FacilityName") or "Facility"
    header = f"# {name} (Recreation.gov facility {payload['FacilityID']})"
    return f"{header}\n\n## [R1] RIDB facility record\n\n" + "\n\n".join(sections)
