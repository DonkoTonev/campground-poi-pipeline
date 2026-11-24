"""OpenStreetMap via Overpass: `tourism=camp_site|caravan_site` nodes, ways and relations.

One query per region (`out body geom`), cached in the raw store. Polygons are kept;
each record also gets a representative point (guaranteed inside its polygon).
Map data © OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from poi.fetch import FetchContext, fetch
from poi.settings import Region, Settings
from poi.sources.base import FetchedRecord, SourceRecord

TOURISM_VALUES = ("camp_site", "caravan_site")


def build_query(bbox: tuple[float, float, float, float], timeout_s: int = 180) -> str:
    south, west, north, east = bbox
    values = "|".join(TOURISM_VALUES)
    return (
        f"[out:json][timeout:{timeout_s}];"
        f'(nwr["tourism"~"^({values})$"]({south},{west},{north},{east}););'
        "out body geom;"  # `body` keeps relation members; `tags` would drop them
    )


def _line(coords: list[dict[str, float]]) -> list[tuple[float, float]]:
    return [(c["lon"], c["lat"]) for c in coords]


def _way_geometry(element: dict[str, Any]) -> BaseGeometry | None:
    coords = _line(element.get("geometry") or [])
    if len(coords) >= 4 and coords[0] == coords[-1]:
        poly = Polygon(coords)
        return poly if poly.is_valid else poly.buffer(0)
    if len(coords) >= 2:
        return LineString(coords)
    return None


def _relation_geometry(element: dict[str, Any]) -> BaseGeometry | None:
    """Assemble a multipolygon relation from its member ways' geometry (outer minus inner)."""
    outer: list[LineString] = []
    inner: list[LineString] = []
    for member in element.get("members", []):
        if member.get("type") != "way" or not member.get("geometry"):
            continue
        line = LineString(_line(member["geometry"]))
        (inner if member.get("role") == "inner" else outer).append(line)
    outer_polys = list(polygonize(outer))
    if not outer_polys:
        return None
    shape = unary_union(outer_polys)
    inner_polys = list(polygonize(inner))
    if inner_polys:
        shape = shape.difference(unary_union(inner_polys))
    return shape if not shape.is_empty else None


def parse(body: bytes) -> Iterator[SourceRecord]:
    for element in json.loads(body).get("elements", []):
        etype, tags = element["type"], element.get("tags", {})
        if tags.get("tourism") not in TOURISM_VALUES:
            continue
        shape: BaseGeometry | None = None
        point: BaseGeometry | None = None
        if etype == "node":
            point = Point(element["lon"], element["lat"])
        elif etype == "way":
            shape = _way_geometry(element)
        elif etype == "relation":
            shape = _relation_geometry(element)
        if shape is not None:
            point = shape.representative_point()
        yield SourceRecord(
            source="osm",
            source_id=f"{etype}/{element['id']}",
            name=tags.get("name"),
            payload={"osm_type": etype, "osm_id": element["id"], "tags": tags},
            point=point,
            shape=shape if shape is not None and shape.geom_type != "Point" else None,
        )


class OsmSource:
    name = "osm"

    def missing_config(self, settings: Settings) -> str | None:
        return None  # public instance, no key

    def records(
        self, ctx: FetchContext, region: Region, settings: Settings
    ) -> Iterator[FetchedRecord]:
        raw = fetch(
            ctx, self.name, "POST", settings.overpass_url, params={"data": build_query(region.bbox)}
        )
        for record in parse(raw.body):
            yield FetchedRecord(raw.raw_object_id, record)
