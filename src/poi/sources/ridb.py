"""Recreation.gov RIDB: federal facilities of type Campground inside the region.

No single RIDB listing is complete (see `RidbSource`), so several are combined and filtered
by type and bbox here. `full=true` embeds the parent rec area (ID and name only).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from typing import Any

from shapely.geometry import Point

from poi.fetch import FetchContext, fetch
from poi.settings import Region, Settings
from poi.sources.base import FetchedRecord, SourceRecord, in_bbox, to_float

PAGE_SIZE = 50
POLITE_DELAY_S = 0.5
# Bulky or irrelevant for POI facts; still available in the raw store.
DROP_KEYS = frozenset({"MEDIA", "EVENT", "TOUR", "PERMITENTRANCE", "CAMPSITE"})


def _coords(lat: Any, lon: Any) -> tuple[float, float] | None:
    la, lo = to_float(lat), to_float(lon)
    if la is None or lo is None or (la == 0 and lo == 0):
        return None
    return la, lo


def _record(facility: dict[str, Any], region_by: str) -> SourceRecord:
    coords = _coords(facility.get("FacilityLatitude"), facility.get("FacilityLongitude"))
    payload = {k: v for k, v in facility.items() if k not in DROP_KEYS}
    payload["_region_by"] = region_by
    return SourceRecord(
        source="ridb",
        source_id=str(facility["FacilityID"]),
        name=facility.get("FacilityName"),
        payload=payload,
        point=Point(coords[1], coords[0]) if coords else None,
    )


def recarea_ids(facility: dict[str, Any]) -> set[str]:
    return {str(ra["RecAreaID"]) for ra in facility.get("RECAREA") or [] if ra.get("RecAreaID")}


def parse(body: bytes, region: Region) -> Iterator[SourceRecord]:
    """Located campgrounds inside the region bbox."""
    for facility in json.loads(body).get("RECDATA", []):
        if facility.get("FacilityTypeDescription") != "Campground":
            continue
        coords = _coords(facility.get("FacilityLatitude"), facility.get("FacilityLongitude"))
        if coords is not None and in_bbox(*coords, region.bbox):
            yield _record(facility, "bbox")


def unlocated_campgrounds(body: bytes) -> list[dict[str, Any]]:
    """Campgrounds without usable coordinates; region membership is decided later."""
    return [
        f
        for f in json.loads(body).get("RECDATA", [])
        if f.get("FacilityTypeDescription") == "Campground"
        and _coords(f.get("FacilityLatitude"), f.get("FacilityLongitude")) is None
    ]


def resolve_unlocated(
    unlocated: list[tuple[int, dict[str, Any]]], home_recareas: set[str]
) -> Iterator[FetchedRecord]:
    """Keep an unlocated campground if its rec area also has located facilities in the region.

    Embedded rec areas carry no coordinates, so membership is the only signal. Large rec
    areas (national forests) extend beyond the bbox, so this can over-include; such records
    keep a null geometry and `_region_by = "recarea"` and are flagged during normalization
    instead of being silently dropped. Name blocking can still match them later.
    """
    for raw_object_id, facility in unlocated:
        if recarea_ids(facility) & home_recareas:
            yield FetchedRecord(raw_object_id, _record(facility, "recarea"))


def bbox_radius_miles(bbox: tuple[float, float, float, float]) -> float:
    """Radius from the bbox centre that reaches its corners (equirectangular, rounded up)."""
    south, west, north, east = bbox
    lat_mi = (north - south) / 2 * 69.0
    lon_mi = (east - west) / 2 * 69.0 * math.cos(math.radians((south + north) / 2))
    return float(math.ceil(math.hypot(lat_mi, lon_mi)))


def total_count(body: bytes) -> int:
    return int(json.loads(body)["METADATA"]["RESULTS"]["TOTAL_COUNT"])


class RidbSource:
    """Union of three listing strategies, because RIDB offset pagination is unstable.

    Paging an unordered result set returns some facilities twice and silently skips others
    (measured on CA: 1,936 rows but 1,887 unique IDs). No single listing is complete, so we
    combine (1) the per-state listing, (2) one radius search around the bbox centre and
    (3) the facility listing of every parent rec area seen in (1)+(2), whose result sets are
    small. Per-strategy contribution is recorded in the run stats.
    """

    name = "ridb"

    def missing_config(self, settings: Settings) -> str | None:
        return None if settings.ridb_api_key else "RIDB_API_KEY not set"

    def records(
        self, ctx: FetchContext, region: Region, settings: Settings
    ) -> Iterator[FetchedRecord]:
        assert settings.ridb_api_key is not None
        headers = {"apikey": settings.ridb_api_key.get_secret_value()}
        seen: set[str] = set()
        home_recareas: set[str] = set()
        unlocated: list[tuple[int, dict[str, Any]]] = []

        def listing(strategy: str, url: str, base: dict[str, Any]) -> Iterator[FetchedRecord]:
            offset, total = 0, None
            returned: list[str] = []
            while total is None or offset < total:
                params = {**base, "full": "true", "limit": PAGE_SIZE, "offset": offset}
                raw = fetch(ctx, self.name, "GET", url, params=params, secret_headers=headers)
                total = total_count(raw.body)
                returned += [str(f["FacilityID"]) for f in json.loads(raw.body)["RECDATA"]]
                unlocated.extend((raw.raw_object_id, f) for f in unlocated_campgrounds(raw.body))
                for record in parse(raw.body, region):
                    home_recareas.update(recarea_ids(record.payload))
                    if record.source_id not in seen:
                        seen.add(record.source_id)
                        ctx.stats[f"found_by_{strategy}"] = (
                            ctx.stats.get(f"found_by_{strategy}", 0) + 1
                        )
                        yield FetchedRecord(raw.raw_object_id, record)
                offset += PAGE_SIZE
                if not raw.from_cache:
                    time.sleep(POLITE_DELAY_S)
            skipped = total - len(set(returned))
            ctx.stats["pagination_skipped"] = ctx.stats.get("pagination_skipped", 0) + skipped

        facilities_url = f"{settings.ridb_base_url}/facilities"
        for state in region.states:
            yield from listing("state", facilities_url, {"state": state})
        south, west, north, east = region.bbox
        centre = {"latitude": (south + north) / 2, "longitude": (west + east) / 2}
        radius = bbox_radius_miles(region.bbox)
        yield from listing("radius", facilities_url, {**centre, "radius": radius})
        for recarea_id in sorted(home_recareas):
            url = f"{settings.ridb_base_url}/recareas/{recarea_id}/facilities"
            yield from listing("recarea", url, {})
        for fetched in resolve_unlocated(unlocated, home_recareas):
            if fetched.record.source_id not in seen:
                seen.add(fetched.record.source_id)
                ctx.stats["unlocated_by_recarea"] = ctx.stats.get("unlocated_by_recarea", 0) + 1
                yield fetched
