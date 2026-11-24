"""Read side shared by the HTTP API and the MCP server: models and queries.

Search goes to Elasticsearch, along-route and detail to PostGIS. Functions take their
connections as arguments and raise `InvalidQueryError` for bad input, so each front end maps
errors to its own protocol (HTTP 422, MCP tool error).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from elasticsearch import Elasticsearch
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Engine, text

from poi.serve import queries

AMENITY_NAMES = (
    "drinking_water",
    "flush_toilets",
    "vault_toilets",
    "showers",
    "electric_hookups",
    "water_hookups",
    "sewer_hookups",
    "dump_station",
    "laundry",
    "camp_store",
    "wifi",
    "fire_rings",
    "picnic_tables",
    "pets_allowed",
    "generators_allowed",
    "ada_accessible",
)
DEFAULT_RADIUS_KM = 50.0


class InvalidQueryError(ValueError):
    """The caller's input is invalid (HTTP 422, MCP tool error)."""


class Location(BaseModel):
    lat: float
    lon: float


class Campground(BaseModel):
    id: str
    name: str | None
    category: str | None
    location: Location | None
    quality_score: float | None
    amenities: list[str] = []
    months_open: list[int] = []
    distance_km: float | None = None
    score: float | None = None


class SearchResult(BaseModel):
    total: int
    items: list[Campground]


class RouteRequest(BaseModel):
    route: list[tuple[float, float]] = Field(
        description="Route as [lon, lat] points, at least two.", min_length=2, max_length=5000
    )
    buffer_km: float = Field(10.0, gt=0, le=100)
    limit: int = Field(100, gt=0, le=1000)

    @model_validator(mode="after")
    def _valid_points(self) -> RouteRequest:
        for lon, lat in self.route:
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError(f"invalid point [{lon}, {lat}] (expected [lon, lat])")
        return self


class RouteStop(BaseModel):
    id: str
    name: str | None
    category: str | None
    location: Location
    quality_score: float | None
    off_route_km: float
    km_from_start: float


class RouteResult(BaseModel):
    route_km: float
    items: list[RouteStop]


class Provenance(BaseModel):
    field: str
    value: Any
    source: str | None
    record: str | None
    reason: str
    alternatives: list[dict[str, Any]]


class CampgroundDetail(BaseModel):
    id: str
    status: str
    name: str | None
    alt_names: list[str]
    category: str | None
    operator_type: str | None
    state_code: str | None
    location: Location | None
    months_open: list[int] | None
    quality: dict[str, Any]
    possibly_closed: bool
    sources: list[str]
    parent_id: str | None
    children: list[str]
    golden: dict[str, Any]
    provenance: list[Provenance]
    members: list[dict[str, str]]
    flags: list[dict[str, Any]]


@dataclass(frozen=True)
class Redirected:
    """The id was merged into another entity."""

    new_id: str


@dataclass(frozen=True)
class Retired:
    """The entity is no longer in any source."""

    id: str


def _hits(response: Any, with_distance: bool) -> SearchResult:
    items = []
    for hit in response["hits"]["hits"]:
        src = hit["_source"]
        distance = None
        if with_distance and hit.get("sort"):
            distance = round(float(hit["sort"][-1]), 2)
        items.append(
            Campground(
                id=src["id"],
                name=src.get("name"),
                category=src.get("category"),
                location=src.get("location"),
                quality_score=src.get("quality_score"),
                amenities=src.get("amenities", []),
                months_open=src.get("months_open", []),
                distance_km=distance,
                score=hit.get("_score"),
            )
        )
    return SearchResult(total=response["hits"]["total"]["value"], items=items)


def search(
    es: Elasticsearch,
    alias: str,
    q: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    amenities: list[str] | None = None,
    open_in_month: int | None = None,
    category: str | None = None,
    size: int = 20,
) -> SearchResult:
    """Fuzzy name search with filters; with a point, ranked by relevance and distance."""
    if (lat is None) != (lon is None):
        raise InvalidQueryError("lat and lon must be given together")
    unknown = set(amenities or []) - set(AMENITY_NAMES)
    if unknown:
        raise InvalidQueryError(f"unknown amenities: {sorted(unknown)}")
    if lat is not None and radius_km is None:
        radius_km = DEFAULT_RADIUS_KM
    response = es.search(
        index=alias,
        query=queries.search_query(
            q, lat, lon, radius_km, amenities or [], open_in_month, category
        ),
        sort=queries.distance_sort(lat, lon),
        size=size,
        track_total_hits=True,
    )
    return _hits(response, with_distance=lat is not None)


def bbox(
    es: Elasticsearch,
    alias: str,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    size: int = 200,
) -> SearchResult:
    if min_lat >= max_lat or min_lon >= max_lon:
        raise InvalidQueryError("min_lat/min_lon must be below max_lat/max_lon")
    response = es.search(
        index=alias,
        query=queries.bbox_query(min_lat, min_lon, max_lat, max_lon),
        sort=[{"quality_score": "desc"}],
        size=size,
        track_total_hits=True,
    )
    return _hits(response, with_distance=False)


def along_route(engine: Engine, region: str, request: RouteRequest) -> RouteResult:
    """Campgrounds within `buffer_km` of a route, in driving order (PostGIS)."""
    wkt = "LINESTRING(" + ", ".join(f"{lon} {lat}" for lon, lat in request.route) + ")"
    with engine.connect() as conn:
        route_km = conn.execute(
            text("SELECT ST_Length(ST_GeomFromText(:w, 4326)::geography) / 1000"),
            {"w": wkt},
        ).scalar_one()
        rows = conn.execute(
            text(
                """
                WITH route AS (SELECT ST_GeomFromText(:w, 4326) AS g)
                SELECT e.id::text AS id, e.name, e.category, e.quality_score,
                       ST_Y(e.geom) AS lat, ST_X(e.geom) AS lon,
                       ST_Distance(e.geom::geography, r.g::geography) / 1000 AS off_km,
                       ST_LineLocatePoint(r.g, e.geom) AS position
                FROM entities e, route r
                WHERE e.region = :region AND e.status = 'active' AND e.geom IS NOT NULL
                  AND ST_DWithin(e.geom::geography, r.g::geography, :buffer_m)
                ORDER BY position, off_km LIMIT :limit
                """
            ),
            {
                "w": wkt,
                "region": region,
                "buffer_m": request.buffer_km * 1000,
                "limit": request.limit,
            },
        ).all()
    return RouteResult(
        route_km=round(float(route_km), 2),
        items=[
            RouteStop(
                id=r.id,
                name=r.name,
                category=r.category,
                location=Location(lat=r.lat, lon=r.lon),
                quality_score=r.quality_score,
                off_route_km=round(float(r.off_km), 2),
                km_from_start=round(float(r.position) * float(route_km), 2),
            )
            for r in rows
        ],
    )


def campground(engine: Engine, entity_id: str) -> CampgroundDetail | Redirected | Retired | None:
    """One golden record with provenance, members, flags; None for an unknown id."""
    try:
        uuid.UUID(entity_id)
    except ValueError:
        return None
    params = {"i": entity_id}
    with engine.connect() as conn:
        redirect = conn.execute(
            text("SELECT new_id::text FROM entity_redirects WHERE old_id = CAST(:i AS uuid)"),
            params,
        ).scalar()
        if redirect:
            return Redirected(redirect)
        row = (
            conn.execute(
                text(
                    "SELECT id::text AS id, status, name, alt_names, category, operator_type, "
                    "state_code, ST_Y(geom) AS lat, ST_X(geom) AS lon, months_open, quality, "
                    "possibly_closed, sources, parent_id::text AS parent_id, golden "
                    "FROM entities WHERE id = CAST(:i AS uuid)"
                ),
                params,
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if row["status"] == "retired":
            return Retired(entity_id)
        provenance = (
            conn.execute(
                text(
                    "SELECT field, value, source, record_ref AS record, reason, alternatives "
                    "FROM entity_fields WHERE entity_id = CAST(:i AS uuid) ORDER BY field"
                ),
                params,
            )
            .mappings()
            .all()
        )
        members = (
            conn.execute(
                text(
                    "SELECT m.record_ref AS record, p.source FROM entity_members m "
                    "JOIN poi_records p ON p.id = m.poi_record_id "
                    "WHERE m.entity_id = CAST(:i AS uuid) ORDER BY 1"
                ),
                params,
            )
            .mappings()
            .all()
        )
        flags = (
            conn.execute(
                text(
                    """
                    SELECT q.stage, q.field, q.rule, q.severity, q.details FROM quality_flags q
                    WHERE q.entity_id = CAST(:i AS uuid) OR q.subject_id IN (
                        SELECT poi_record_id FROM entity_members WHERE entity_id = CAST(:i AS uuid))
                    ORDER BY q.severity, q.rule
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
        children = (
            conn.execute(
                text(
                    "SELECT id::text FROM entities WHERE parent_id = CAST(:i AS uuid) "
                    "AND status = 'active' ORDER BY 1"
                ),
                params,
            )
            .scalars()
            .all()
        )
    return CampgroundDetail(
        id=row["id"],
        status=row["status"],
        name=row["name"],
        alt_names=row["alt_names"],
        category=row["category"],
        operator_type=row["operator_type"],
        state_code=row["state_code"],
        location=Location(lat=row["lat"], lon=row["lon"]) if row["lat"] is not None else None,
        months_open=row["months_open"],
        quality=row["quality"],
        possibly_closed=row["possibly_closed"],
        sources=row["sources"],
        parent_id=row["parent_id"],
        children=list(children),
        golden=row["golden"],
        provenance=[Provenance(**dict(p)) for p in provenance],
        members=[dict(m) for m in members],
        flags=[dict(f) for f in flags],
    )
