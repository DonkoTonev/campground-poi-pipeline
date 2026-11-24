"""MCP server end to end: the same served fixture pipeline, called through an in-process client."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from elasticsearch import Elasticsearch
from mcp import Client
from sqlalchemy import Engine, text

from conftest import entity_id
from poi.serve.mcp_server import build_server

pytestmark = pytest.mark.integration


def _call(server: Any, tool: str, args: dict[str, Any]) -> Any:
    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


@pytest.fixture
def server(db: Engine, es: Elasticsearch, served: str) -> Any:
    return build_server(db, es, "sierra", served)


def test_lists_three_read_only_tools(server: Any) -> None:
    async def go() -> Any:
        async with Client(server) as client:
            return await client.list_tools()

    tools = {t.name: t for t in asyncio.run(go()).tools}
    assert set(tools) == {"search_campgrounds", "get_campground", "along_route"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())
    assert "longitude first" in str(tools["along_route"].input_schema)


def test_search_with_typo_and_filters(server: Any) -> None:
    result = _call(server, "search_campgrounds", {"query": "uper pines"})
    assert not result.is_error
    assert result.structured_content["items"][0]["name"] == "Upper Pines Campground"
    result = _call(server, "search_campgrounds", {"query": "pines", "open_in_month": 1})
    assert [i["name"] for i in result.structured_content["items"]] == ["Upper Pines Campground"]
    bad = _call(server, "search_campgrounds", {"amenities": ["jacuzzi"]})
    assert bad.is_error and "unknown amenities" in bad.content[0].text


def test_get_campground_follows_merges_and_reports_errors(server: Any, db: Engine) -> None:
    survivor = entity_id(db, "Upper Pines Campground")
    detail = _call(server, "get_campground", {"campground_id": survivor}).structured_content
    assert detail["sources"] == ["nps", "osm", "ridb"] and detail["provenance"]
    old = str(uuid.uuid4())
    with db.begin() as conn:
        run = conn.execute(text("SELECT max(id) FROM runs")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO entities (id, region, status, created_run_id, updated_run_id) "
                "VALUES (CAST(:o AS uuid), 'sierra', 'merged', :r, :r)"
            ),
            {"o": old, "r": run},
        )
        conn.execute(
            text(
                "INSERT INTO entity_redirects (old_id, new_id, reason, run_id) "
                "VALUES (CAST(:o AS uuid), CAST(:n AS uuid), 'merged', :r)"
            ),
            {"o": old, "n": survivor, "r": run},
        )
    moved = _call(server, "get_campground", {"campground_id": old})
    assert moved.structured_content["id"] == survivor
    unknown = _call(server, "get_campground", {"campground_id": "not-an-id"})
    assert unknown.is_error and "unknown campground id" in unknown.content[0].text


def test_along_route(server: Any) -> None:
    route = [[-119.60, 37.73], [-119.55, 37.74], [-119.36, 37.87]]
    result = _call(server, "along_route", {"route": route, "buffer_km": 2})
    names = [i["name"] for i in result.structured_content["items"]]
    assert names.index("Upper Pines Campground") < names.index("Tuolumne Meadows Campground")
    swapped = _call(server, "along_route", {"route": [[37.73, -119.60], [37.74, -119.55]]})
    assert swapped.is_error and "expected [lon, lat]" in swapped.content[0].text
