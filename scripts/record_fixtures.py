"""Cut small, scrubbed test fixtures out of the raw store (run after `poi ingest`).

Fixtures are real public records (OSM / RIDB / NPS) chosen for their edge-case value, trimmed
to a few elements, with long strings shortened and contact data replaced (poi.privacy).
Re-running is deterministic for the same raw data.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from sqlalchemy import text

from poi.db import get_engine
from poi.privacy import scrub
from poi.settings import PROJECT_ROOT, get_settings
from poi.storage import make_store

OUT = PROJECT_ROOT / "tests" / "fixtures"
PINES = ("Upper Pines", "Lower Pines", "North Pines")
OSM_NAMES = {f"{p} Campground" for p in PINES} | {"Tuolumne Meadows Campground"}
NPS_NAMES = OSM_NAMES | {"Tuolumne Meadows Horse Camp"}
MAX_STR = 200


def _trim(obj: Any) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_STR else obj[:MAX_STR] + "…"
    if isinstance(obj, list):
        return [_trim(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _trim(v) for k, v in obj.items()}
    return obj


def _bodies(source: str, latest_only: bool = False) -> list[dict[str, Any]]:
    """Latest response per request; with `latest_only`, just the newest response overall
    (e.g. OSM, where an older query version must not leak into fixtures)."""
    store = make_store(get_settings())
    with get_engine().connect() as conn:
        keys = conn.execute(
            text(
                "SELECT storage_key FROM (SELECT DISTINCT ON (request_key) storage_key, "
                "fetched_at FROM raw_objects WHERE source = :s AND http_status = 200 "
                "ORDER BY request_key, fetched_at DESC) t ORDER BY fetched_at DESC "
                "LIMIT :n"
            ),
            {"s": source, "n": 1 if latest_only else 10_000},
        ).scalars()
        return [json.loads(gzip.decompress(store.get(k))) for k in keys]


def _write(name: str, data: Any) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(_trim(data)), indent=1, sort_keys=True) + "\n")
    print(f"wrote {path.relative_to(PROJECT_ROOT)}")


def main() -> None:
    elements = [e for body in _bodies("osm", latest_only=True) for e in body["elements"]]
    picked = [e for e in elements if e.get("tags", {}).get("name") in OSM_NAMES]
    picked += [e for e in elements if e["type"] == "node"][:2]  # plain nodes, one may be unnamed
    for e in picked:
        e.pop("nodes", None)  # node refs are noise for our parser
    _write("osm/overpass.json", {"elements": picked})

    facilities = [f for body in _bodies("ridb") for f in body["RECDATA"]]
    by_name = {f["FacilityName"].upper(): f for f in facilities}
    ridb = [by_name[n] for n in sorted(by_name) if any(p.upper() in n for p in PINES)]
    ridb += [next(f for f in facilities if f["FacilityTypeDescription"] != "Campground")]
    ridb += [
        next(
            f
            for f in facilities
            if f["FacilityTypeDescription"] == "Campground"
            and (f.get("FacilityLatitude") or 0) < 37.0
        )
    ]  # outside the sierra bbox
    for f in ridb:
        for key in ("MEDIA", "EVENT", "TOUR", "PERMITENTRANCE", "CAMPSITE"):
            f.pop(key, None)
    _write(
        "ridb/facilities.json",
        {
            "METADATA": {"RESULTS": {"CURRENT_COUNT": len(ridb), "TOTAL_COUNT": len(ridb)}},
            "RECDATA": ridb,
        },
    )

    camps = [c for body in _bodies("nps") for c in body["data"] if c["name"] in NPS_NAMES]
    for c in camps:
        c.pop("images", None)
        c.pop("multimedia", None)
    _write(
        "nps/campgrounds.json",
        {"total": str(len(camps)), "limit": "50", "start": "0", "data": camps},
    )


if __name__ == "__main__":
    main()
