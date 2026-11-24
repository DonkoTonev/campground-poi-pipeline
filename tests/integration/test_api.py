"""API end to end: fixture pipeline → PostGIS + a test Elasticsearch alias → FastAPI."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import httpx
import pytest
import respx
from elasticsearch import Elasticsearch
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from conftest import load_fixture
from poi.ingest import ingest_source
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.resolve.stage import resolve_region
from poi.serve.api import create_app
from poi.serve.index import build
from poi.settings import Settings, get_settings, load_region
from poi.sources import ridb
from poi.storage.local import LocalStore

pytestmark = pytest.mark.integration
SIERRA = load_region("sierra")
UPPER_PINES = (37.7356, -119.5635)


@pytest.fixture
def es() -> Iterator[Elasticsearch]:
    client = Elasticsearch(get_settings().elasticsearch_url)
    try:
        if not client.ping():
            pytest.skip("Elasticsearch not reachable (run `make up`)")
    except Exception:
        pytest.skip("Elasticsearch not reachable (run `make up`)")
    yield client
    client.close()


@pytest.fixture
def api(
    db: Engine, es: Elasticsearch, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(ridb, "POLITE_DELAY_S", 0)
    store = LocalStore(settings.raw_store_path)
    with respx.mock:
        respx.post(settings.overpass_url).mock(
            return_value=httpx.Response(200, json=load_fixture("osm/overpass.json"))
        )
        respx.get(url__startswith=settings.ridb_base_url).mock(
            return_value=httpx.Response(200, json=load_fixture("ridb/facilities.json"))
        )
        respx.get(url__startswith=settings.nps_base_url).mock(
            return_value=httpx.Response(200, json=load_fixture("nps/campgrounds.json"))
        )
        for source in ("osm", "ridb", "nps"):
            ingest_source(db, store, settings, SIERRA, source)
    normalize_region(db, SIERRA, date(2026, 11, 24))
    match_region(db, SIERRA)
    resolve_region(db, SIERRA)
    alias = f"test-campgrounds-{uuid.uuid4().hex[:8]}"
    build(db, es, SIERRA, alias=alias)
    yield TestClient(create_app(engine=db, es=es, region="sierra", alias=alias))
    for index in es.indices.get(index=f"{alias}-v*"):  # deletes need explicit names
        es.indices.delete(index=index)


def _id(db: Engine, name: str) -> str:
    with db.connect() as conn:
        return str(
            conn.execute(
                text("SELECT id::text FROM entities WHERE name = :n AND status = 'active'"),
                {"n": name},
            ).scalar_one()
        )


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
