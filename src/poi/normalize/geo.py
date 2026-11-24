"""Coordinate validation against the region and admin-1 (state/province) boundaries.

Checks run on the *raw* coordinates a source sent, so problems that a parser turned into a
null geometry (such as 0,0) are still reported. Nothing is corrected silently: a swapped or
sign-flipped pair is flagged with the suggested fix in the flag details.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from poi.normalize.model import Flag, Severity
from poi.settings import PROJECT_ROOT, Region

ADMIN1_PATH = PROJECT_ROOT / "data" / "reference" / "admin1_us_ca.geojson"
STATE_TOLERANCE_DEG = 0.02  # ~2 km: boundaries are simplified, campgrounds sit on borders
NA_LAT = (14.0, 84.0)
NA_LON = (-170.0, -50.0)
METERS_PER_DEGREE = 111_320.0
LOW_PRECISION_DECIMALS = 2


@dataclass(frozen=True)
class Admin1:
    codes: list[str]
    names: list[str]
    geoms: list[BaseGeometry]
    tree: STRtree

    def code_for(self, value: str) -> str | None:
        """Accept "CA" or "California" (any case); None if unknown."""
        text = value.strip()
        if text.upper() in self.codes:
            return text.upper()
        lowered = [n.lower() for n in self.names]
        return self.codes[lowered.index(text.lower())] if text.lower() in lowered else None

    def locate(self, point: Point) -> str | None:
        for idx in self.tree.query(point):
            if self.geoms[int(idx)].contains(point):
                return self.codes[int(idx)]
        return None

    def near(self, point: Point, code: str) -> bool:
        return any(
            self.codes[int(i)] == code and self.geoms[int(i)].distance(point) <= STATE_TOLERANCE_DEG
            for i in self.tree.query(point.buffer(STATE_TOLERANCE_DEG))
        )


@lru_cache
def admin1() -> Admin1:
    features = json.loads(ADMIN1_PATH.read_text())["features"]
    codes = [f["properties"]["code"] for f in features]
    names = [f["properties"]["name"] for f in features]
    geoms = [shape(f["geometry"]) for f in features]
    return Admin1(codes, names, geoms, STRtree(geoms))


def decimals(value: Any) -> int | None:
    """Decimal places as the source wrote them ("37.70" has 2; 37.7 has 1)."""
    try:
        exponent = Decimal(str(value).strip()).as_tuple().exponent
    except (InvalidOperation, ValueError):
        return None
    return max(0, -exponent) if isinstance(exponent, int) else None


def _in(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


@dataclass(frozen=True)
class GeoResult:
    state_code: str | None
    precision_m: float | None
    flags: list[Flag]


def check_coordinates(
    raw_lat: Any, raw_lon: Any, region: Region, claimed_state: str | None
) -> GeoResult:
    flags: list[Flag] = []
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except (TypeError, ValueError):
        flags.append(Flag("geometry", "missing_coordinates", Severity.WARNING))
        return GeoResult(claimed_state, None, flags)

    if lat == 0 and lon == 0:
        flags.append(Flag("geometry", "null_island", Severity.ERROR))
        return GeoResult(claimed_state, None, flags)
    # Diagnose the specific mistake before the generic range check: a swapped pair usually
    # has |lat| > 90 and would otherwise only be reported as "out of range".
    if not (_in(lat, NA_LAT) and _in(lon, NA_LON)):
        if _in(lon, NA_LAT) and _in(lat, NA_LON):
            flags.append(
                Flag("geometry", "swapped_lat_lon", Severity.ERROR, {"suggested": [lon, lat]})
            )
        elif lon > 0 and _in(-lon, NA_LON) and _in(lat, NA_LAT):
            flags.append(
                Flag("geometry", "positive_longitude", Severity.ERROR, {"suggested": [lat, -lon]})
            )
        elif not (-90 <= lat <= 90 and -180 <= lon <= 180):
            flags.append(Flag("geometry", "out_of_range", Severity.ERROR, {"lat": lat, "lon": lon}))
        else:
            flags.append(Flag("geometry", "outside_north_america", Severity.ERROR))
        return GeoResult(claimed_state, None, flags)

    places = min(d for d in (decimals(raw_lat), decimals(raw_lon)) if d is not None)
    precision_m = METERS_PER_DEGREE * 10.0**-places
    if places <= LOW_PRECISION_DECIMALS:
        flags.append(
            Flag(
                "geometry",
                "low_precision",
                Severity.WARNING,
                {"decimals": places, "precision_m": precision_m},
            )
        )

    south, west, north, east = region.bbox
    if not (south <= lat <= north and west <= lon <= east):
        flags.append(Flag("geometry", "outside_region", Severity.WARNING, {"region": region.name}))

    point = Point(lon, lat)
    located = admin1().locate(point)
    if claimed_state and not admin1().near(point, claimed_state):
        flags.append(
            Flag(
                "geometry",
                "outside_claimed_state",
                Severity.WARNING,
                {"claimed": claimed_state, "located": located},
            )
        )
    return GeoResult(claimed_state or located, precision_m, flags)
