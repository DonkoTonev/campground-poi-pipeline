"""FastAPI app: search and map queries from Elasticsearch, records and routes from PostGIS.

Run with `poi serve` (or `uvicorn poi.serve.api:app`); OpenAPI docs at /docs. The queries live
in `poi.serve.service`, shared with the MCP server.
"""

from __future__ import annotations

from typing import Annotated, Any

from elasticsearch import Elasticsearch
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import Engine, text

from poi.db import get_engine
from poi.log import get_logger
from poi.serve import service
from poi.serve.index import alias_name
from poi.serve.service import (
    CampgroundDetail,
    InvalidQueryError,
    RouteRequest,
    RouteResult,
    SearchResult,
)
from poi.settings import get_settings

log = get_logger(__name__)


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

    @app.exception_handler(InvalidQueryError)
    def invalid_query(_: Request, exc: InvalidQueryError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

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
        return service.search(
            es, request_alias(app), q, lat, lon, radius_km, amenities, open_in_month, category, size
        )

    @app.get("/campgrounds/bbox", response_model=SearchResult)
    def bbox(
        es: Annotated[Elasticsearch, Depends(search_client)],
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        size: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> SearchResult:
        return service.bbox(es, request_alias(app), min_lat, min_lon, max_lat, max_lon, size)

    @app.post("/campgrounds/along-route", response_model=RouteResult)
    def along_route(body: RouteRequest, engine: Annotated[Engine, Depends(db)]) -> RouteResult:
        """Campgrounds within `buffer_km` of a route, in driving order (PostGIS)."""
        return service.along_route(engine, app.state.region, body)

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
        result = service.campground(engine, entity_id)
        if result is None:
            raise HTTPException(404, "unknown campground id")
        if isinstance(result, service.Redirected):
            return RedirectResponse(f"/campgrounds/{result.new_id}", status_code=301)
        if isinstance(result, service.Retired):
            return JSONResponse({"id": result.id, "status": "retired"}, status_code=410)
        return result

    return app


def request_alias(app: FastAPI) -> str:
    alias: str = app.state.alias
    return alias


def __getattr__(name: str) -> Any:
    """`uvicorn poi.serve.api:app` builds the app lazily (no DB/ES connection at import)."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
