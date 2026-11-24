"""NPS API `/campgrounds` for the region's park codes. Rich structured amenities."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from shapely.geometry import Point

from poi.fetch import FetchContext, fetch
from poi.settings import Region, Settings
from poi.sources.base import FetchedRecord, SourceRecord, to_float

PAGE_SIZE = 50
POLITE_DELAY_S = 0.5
DROP_KEYS = frozenset({"images", "multimedia"})


def parse(body: bytes) -> Iterator[SourceRecord]:
    for camp in json.loads(body).get("data", []):
        lat, lon = to_float(camp.get("latitude")), to_float(camp.get("longitude"))
        point = None
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            point = Point(lon, lat)
        yield SourceRecord(
            source="nps",
            source_id=str(camp["id"]),
            name=camp.get("name"),
            payload={k: v for k, v in camp.items() if k not in DROP_KEYS},
            point=point,
        )


class NpsSource:
    name = "nps"

    def missing_config(self, settings: Settings) -> str | None:
        return None if settings.nps_api_key else "NPS_API_KEY not set"

    def records(
        self, ctx: FetchContext, region: Region, settings: Settings
    ) -> Iterator[FetchedRecord]:
        if not region.nps_park_codes:
            return
        assert settings.nps_api_key is not None
        headers = {"X-Api-Key": settings.nps_api_key.get_secret_value()}
        url = f"{settings.nps_base_url}/campgrounds"
        start, total = 0, None
        while total is None or start < total:
            params = {
                "parkCode": ",".join(region.nps_park_codes),
                "limit": PAGE_SIZE,
                "start": start,
            }
            raw = fetch(ctx, self.name, "GET", url, params=params, secret_headers=headers)
            total = int(json.loads(raw.body).get("total", 0))
            for record in parse(raw.body):
                yield FetchedRecord(raw.raw_object_id, record)
            start += PAGE_SIZE
            if not raw.from_cache:
                time.sleep(POLITE_DELAY_S)
