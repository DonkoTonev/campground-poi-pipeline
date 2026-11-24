"""FastAPI app: search and map queries from Elasticsearch, records and routes from PostGIS.

Run with `poi serve` (or `uvicorn poi.serve.api:app`); OpenAPI docs at /docs.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from elasticsearch import Elasticsearch
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Engine, text

from poi.db import get_engine
from poi.log import get_logger
from poi.serve import queries
from poi.serve.index import alias_name
from poi.settings import get_settings

log = get_logger(__name__)

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


# Dependencies live at module level: with postponed annotations FastAPI resolves
# `Annotated[..., Depends(...)]` through module globals, so closures would not be found.
def db(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def search_client(request: Request) -> Elasticsearch:
    client: Elasticsearch = request.app.state.es
    return client


def create_app(
    engine: Engine | None = None,
    es: Elasticsearch | None = None,
    region: str | None = None,
    alias: str | None = None,
) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Campground POI API",
        version="0.1.0",
        description="Golden campground records with provenance: search, map, along-route.",
    )
    app.state.engine = engine or get_engine()
    app.state.es = es or Elasticsearch(settings.elasticsearch_url)
    app.state.alias = alias or alias_name(region or settings.region)
    app.state.region = region or settings.region

    @app.get("/health")
    def health(
        engine: Annotated[Engine, Depends(db)], es: Annotated[Elasticsearch, Depends(search_client)]
    ) -> JSONResponse:
        status = {"db": False, "search": False}
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            status["db"] = True
        except Exception as exc:  # health reports failures, it doesn't raise them
            log.warning("health.db_unavailable", error=str(exc)[:200])
        try:
            status["search"] = bool(es.ping())
        except Exception as exc:
            log.warning("health.search_unavailable", error=str(exc)[:200])
        ok = all(status.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", **status}, status_code=200 if ok else 503
        )

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

    @app.get("/campgrounds/search", response_model=SearchResult)
    def search(
        es: Annotated[Elasticsearch, Depends(search_client)],
        q: Annotated[str | None, Query(max_length=200)] = None,
        lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
        lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
        radius_km: Annotated[float | None, Query(gt=0, le=1000)] = None,
        amenities: Annotated[list[str] | None, Query()] = None,
        open_in_month: Annotated[int | None, Query(ge=1, le=12)] = None,
        category: str | None = None,
        size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> SearchResult:
        if (lat is None) != (lon is None):
            raise HTTPException(422, "lat and lon must be given together")
        unknown = set(amenities or []) - set(AMENITY_NAMES)
        if unknown:
            raise HTTPException(422, f"unknown amenities: {sorted(unknown)}")
        if lat is not None and radius_km is None:
            radius_km = 50.0
        response = es.search(
            index=request_alias(app),
            query=queries.search_query(
                q, lat, lon, radius_km, amenities or [], open_in_month, category
            ),
            sort=queries.distance_sort(lat, lon),
            size=size,
            track_total_hits=True,
        )
        return _hits(response, with_distance=lat is not None)

    @app.get("/campgrounds/bbox", response_model=SearchResult)
    def bbox(
        es: Annotated[Elasticsearch, Depends(search_client)],
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        size: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> SearchResult:
        if min_lat >= max_lat or min_lon >= max_lon:
            raise HTTPException(422, "min_lat/min_lon must be below max_lat/max_lon")
        response = es.search(
            index=request_alias(app),
            query=queries.bbox_query(min_lat, min_lon, max_lat, max_lon),
            sort=[{"quality_score": "desc"}],
            size=size,
            track_total_hits=True,
        )
        return _hits(response, with_distance=False)

    @app.post("/campgrounds/along-route", response_model=RouteResult)
    def along_route(body: RouteRequest, engine: Annotated[Engine, Depends(db)]) -> RouteResult:
        """Campgrounds within `buffer_km` of a route, in driving order (PostGIS)."""
        wkt = "LINESTRING(" + ", ".join(f"{lon} {lat}" for lon, lat in body.route) + ")"
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
                    "region": app.state.region,
                    "buffer_m": body.buffer_km * 1000,
                    "limit": body.limit,
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

    @app.get(
        "/campgrounds/{entity_id}",
        response_model=CampgroundDetail,
        responses={
            301: {"description": "Merged: redirects to the surviving id"},
            404: {"description": "Unknown id"},
            410: {"description": "Retired: no longer in any source"},
        },
    )
    def get_campground(entity_id: str, engine: Annotated[Engine, Depends(db)]) -> Any:
        try:
            uuid.UUID(entity_id)
        except ValueError as exc:
            raise HTTPException(404, "unknown campground id") from exc
        with engine.connect() as conn:
            redirect = conn.execute(
                text("SELECT new_id::text FROM entity_redirects WHERE old_id = CAST(:i AS uuid)"),
                {"i": entity_id},
            ).scalar()
            if redirect:
                return RedirectResponse(f"/campgrounds/{redirect}", status_code=301)
            row = (
                conn.execute(
                    text(
                        "SELECT id::text AS id, status, name, alt_names, category, operator_type, "
                        "state_code, ST_Y(geom) AS lat, ST_X(geom) AS lon, months_open, quality, "
                        "possibly_closed, sources, parent_id::text AS parent_id, golden "
                        "FROM entities WHERE id = CAST(:i AS uuid)"
                    ),
                    {"i": entity_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "unknown campground id")
            if row["status"] == "retired":
                return JSONResponse({"id": entity_id, "status": "retired"}, status_code=410)
            provenance = (
                conn.execute(
                    text(
                        "SELECT field, value, source, record_ref AS record, reason, alternatives "
                        "FROM entity_fields WHERE entity_id = CAST(:i AS uuid) ORDER BY field"
                    ),
                    {"i": entity_id},
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
                    {"i": entity_id},
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
                    {"i": entity_id},
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
                    {"i": entity_id},
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

    return app


def request_alias(app: FastAPI) -> str:
    alias: str = app.state.alias
    return alias


def __getattr__(name: str) -> Any:
    """`uvicorn poi.serve.api:app` builds the app lazily (no DB/ES connection at import)."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
