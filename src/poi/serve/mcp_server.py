"""MCP server: the campground read API as tools an assistant can call.

Three read-only tools over the same queries as the HTTP API (`poi.serve.service`):
`search_campgrounds` (Elasticsearch), `get_campground` and `along_route` (PostGIS).
Run with `poi mcp` (stdio transport); see the README for connecting it to Claude.
"""

from __future__ import annotations

from typing import Annotated

from elasticsearch import Elasticsearch
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from sqlalchemy import Engine

from poi.normalize.model import Category
from poi.serve import service
from poi.serve.service import (
    AMENITY_NAMES,
    CampgroundDetail,
    InvalidQueryError,
    RouteRequest,
    RouteResult,
    SearchResult,
)

MAX_REDIRECTS = 5
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Campground points of interest for one region, deduplicated across OpenStreetMap, Recreation.gov
and the National Park Service, with field-level provenance. Use search_campgrounds to find
campgrounds by name, place or filters, get_campground for one record with where each value came
from, and along_route for campgrounds near a driving route in driving order. Coordinates are
WGS84; routes are [lon, lat] pairs. Unknown facts are missing, never "no": an amenity absent
from a result is unknown unless the detail says otherwise."""


def build_server(engine: Engine, es: Elasticsearch, region: str, alias: str) -> MCPServer:
    server: MCPServer = MCPServer(name="campgrounds", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    def search_campgrounds(
        query: Annotated[
            str | None,
            Field(description="Name or part of it; typos are tolerated.", max_length=200),
        ] = None,
        near_lat: Annotated[
            float | None, Field(description="Latitude to search around.", ge=-90, le=90)
        ] = None,
        near_lon: Annotated[
            float | None, Field(description="Longitude to search around.", ge=-180, le=180)
        ] = None,
        radius_km: Annotated[
            float | None,
            Field(description="Radius around the point (default 50 km).", gt=0, le=1000),
        ] = None,
        amenities: Annotated[
            list[str] | None,
            Field(description=f"Required amenities, any of: {', '.join(AMENITY_NAMES)}."),
        ] = None,
        open_in_month: Annotated[
            int | None, Field(description="Only campgrounds known to be open (1-12).", ge=1, le=12)
        ] = None,
        category: Annotated[
            str | None,
            Field(description=f"One of: {', '.join(c.value for c in Category)}."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
    ) -> SearchResult:
        """Find campgrounds by name, location and filters, best matches first.

        With a point, results are ranked by relevance and distance and carry distance_km.
        """
        try:
            return service.search(
                es,
                alias,
                query,
                near_lat,
                near_lon,
                radius_km,
                amenities,
                open_in_month,
                category,
                limit,
            )
        except InvalidQueryError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    def get_campground(
        campground_id: Annotated[str, Field(description="Id from search_campgrounds.")],
    ) -> CampgroundDetail:
        """One campground: chosen values, every source's alternative, members and quality flags.

        An id that was merged into another campground resolves to the surviving record (its id
        differs from the one requested).
        """
        current = campground_id
        for _ in range(MAX_REDIRECTS):
            result = service.campground(engine, current)
            if isinstance(result, service.Redirected):
                current = result.new_id
                continue
            if result is None:
                raise ToolError(f"unknown campground id {campground_id}")
            if isinstance(result, service.Retired):
                raise ToolError(
                    f"campground {campground_id} is retired: no source lists it anymore"
                )
            return result
        raise ToolError(f"too many redirects for {campground_id}")

    @server.tool(annotations=READ_ONLY)
    def along_route(
        route: Annotated[
            list[tuple[float, float]],
            Field(description="The route as [lon, lat] points (longitude first), at least two."),
        ],
        buffer_km: Annotated[
            float, Field(description="Maximum distance from the route.", gt=0, le=100)
        ] = 10.0,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> RouteResult:
        """Campgrounds within buffer_km of a route, in driving order with km_from_start."""
        try:
            request = RouteRequest(route=route, buffer_km=buffer_km, limit=limit)
        except ValidationError as exc:
            raise ToolError(str(exc)) from exc
        return service.along_route(engine, region, request)

    return server
