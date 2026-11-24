"""API end to end: fixture pipeline → PostGIS + a test Elasticsearch alias → FastAPI."""

from __future__ import annotations

import uuid

import pytest
from elasticsearch import Elasticsearch
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from conftest import entity_id as _id
from poi.serve.api import create_app

pytestmark = pytest.mark.integration
UPPER_PINES = (37.7356, -119.5635)


@pytest.fixture
def api(db: Engine, es: Elasticsearch, served: str) -> TestClient:
    return TestClient(create_app(engine=db, es=es, region="sierra", alias=served))


def test_health(api: TestClient) -> None:
    assert api.get("/health").json() == {"status": "ok", "db": True, "search": True}


def test_fuzzy_search_and_distance_sort(api: TestClient) -> None:
    r = api.get("/campgrounds/search", params={"q": "uper pines"}).json()  # typo
    assert r["items"][0]["name"] == "Upper Pines Campground"
    lat, lon = UPPER_PINES
    r = api.get("/campgrounds/search", params={"lat": lat, "lon": lon, "radius_km": 3}).json()
    names = [i["name"] for i in r["items"]]
    assert {"Upper Pines Campground", "Lower Pines Campground", "North Pines Campground"} <= set(
        names
    )
    assert all(i["distance_km"] is not None and i["distance_km"] <= 3 for i in r["items"])


def test_filters(api: TestClient) -> None:
    r = api.get("/campgrounds/search", params={"amenities": ["dump_station"]}).json()
    assert "Upper Pines Campground" in [i["name"] for i in r["items"]]
    r = api.get("/campgrounds/search", params={"open_in_month": 1, "q": "pines"}).json()
    assert [i["name"] for i in r["items"]] == ["Upper Pines Campground"]  # only one open all year
    r = api.get("/campgrounds/search", params={"category": "horse"}).json()
    assert r["total"] >= 1 and all(i["category"] == "horse" for i in r["items"])


def test_bbox(api: TestClient) -> None:
    r = api.get(
        "/campgrounds/bbox",
        params={"min_lat": 37.72, "min_lon": -119.58, "max_lat": 37.75, "max_lon": -119.55},
    ).json()
    assert {i["name"] for i in r["items"]} >= {"Upper Pines Campground", "Lower Pines Campground"}
    bad = api.get(
        "/campgrounds/bbox", params={"min_lat": 38, "min_lon": -119, "max_lat": 37, "max_lon": -118}
    )
    assert bad.status_code == 422


def test_along_route_in_driving_order(api: TestClient) -> None:
    # Valley floor eastward, then up to Tuolumne Meadows.
    route = [[-119.60, 37.73], [-119.55, 37.74], [-119.36, 37.87]]
    r = api.post("/campgrounds/along-route", json={"route": route, "buffer_km": 2}).json()
    names = [i["name"] for i in r["items"]]
    assert names.index("Upper Pines Campground") < names.index("Tuolumne Meadows Campground")
    kms = [i["km_from_start"] for i in r["items"]]
    assert kms == sorted(kms) and r["route_km"] > 15


def test_detail_with_provenance(api: TestClient, db: Engine) -> None:
    detail = api.get(f"/campgrounds/{_id(db, 'Upper Pines Campground')}").json()
    assert detail["sources"] == ["nps", "osm", "ridb"]
    by_field = {p["field"]: p for p in detail["provenance"]}
    assert by_field["geometry"]["source"] == "osm"  # polygon wins
    assert {m["source"] for m in detail["members"]} == {"nps", "osm", "ridb"}
    assert detail["quality"]["score"] > 0.9


def test_redirect_retired_and_unknown(api: TestClient, db: Engine) -> None:
    survivor = _id(db, "Upper Pines Campground")
    old = str(uuid.uuid4())
    with db.begin() as conn:
        run = conn.execute(text("SELECT max(id) FROM runs")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO entities (id, region, status, created_run_id, "
                "updated_run_id) VALUES (CAST(:o AS uuid), 'sierra', 'merged', :r, :r)"
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
    moved = api.get(f"/campgrounds/{old}", follow_redirects=False)
    assert moved.status_code == 301 and moved.headers["location"] == f"/campgrounds/{survivor}"
    retired = _id(db, "Lower Pines Campground")
    with db.begin() as conn:
        conn.execute(
            text("UPDATE entities SET status = 'retired' WHERE id = CAST(:i AS uuid)"),
            {"i": retired},
        )
    assert api.get(f"/campgrounds/{retired}").status_code == 410
    assert api.get(f"/campgrounds/{uuid.uuid4()}").status_code == 404
