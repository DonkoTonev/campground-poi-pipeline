"""End-to-end ingest against a real PostGIS test database with mocked HTTP."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, text

from conftest import load_fixture
from poi.ingest import ingest_source
from poi.settings import Settings, load_region
from poi.sources import ridb
from poi.storage.local import LocalStore

pytestmark = pytest.mark.integration
SIERRA = load_region("sierra")


def _rows(db: Engine, source: str) -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT source_id, name, missed_runs, ST_AsText(geom) AS geom, "
                "shape IS NOT NULL AS has_shape FROM source_records WHERE source = :s"
            ),
            {"s": source},
        )
        return {r.source_id: r for r in rows}


@respx.mock
def test_osm_ingest_is_idempotent_and_tracks_missing(db: Engine, settings: Settings) -> None:
    store = LocalStore(settings.raw_store_path)
    data = load_fixture("osm/overpass.json")
    route = respx.post(settings.overpass_url).mock(return_value=httpx.Response(200, json=data))

    first = ingest_source(db, store, settings, SIERRA, "osm")
    assert first["new"] == len(data["elements"]) and first["requests"] == 1
    rows = _rows(db, "osm")
    assert all(r.geom for r in rows.values())
    assert sum(r.has_shape for r in rows.values()) == 4  # 3 Pines ways + Tuolumne relation

    second = ingest_source(db, store, settings, SIERRA, "osm")
    assert second["unchanged"] == len(data["elements"])
    assert second["requests"] == 0 and second["cache_hits"] == 1  # fetch once, process many
    assert route.call_count == 1

    # The source drops one element: it is kept (never deleted) and counted as missed.
    gone = data["elements"][-1]
    route.mock(return_value=httpx.Response(200, json={"elements": data["elements"][:-1]}))
    third = ingest_source(db, store, settings, SIERRA, "osm", refresh=True)
    assert third["missing"] == 1 and third["new"] == 0
    rows = _rows(db, "osm")
    assert rows[f"{gone['type']}/{gone['id']}"].missed_runs == 1
    assert len(rows) == len(data["elements"])


def test_source_without_key_is_skipped(db: Engine, settings: Settings) -> None:
    no_key = settings.model_copy(update={"ridb_api_key": None})
    stats = ingest_source(db, LocalStore(settings.raw_store_path), no_key, SIERRA, "ridb")
    assert stats == {"skipped": "RIDB_API_KEY not set"}
    assert _rows(db, "ridb") == {}


@respx.mock
def test_ridb_combines_listings_and_keeps_unlocated_home_campgrounds(
    db: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ridb, "POLITE_DELAY_S", 0)
    facilities = {f["FacilityName"]: f for f in load_fixture("ridb/facilities.json")["RECDATA"]}

    def page(*names: str) -> httpx.Response:
        recs = [facilities[n] for n in names]
        meta = {"RESULTS": {"CURRENT_COUNT": len(recs), "TOTAL_COUNT": len(recs)}}
        return httpx.Response(200, json={"METADATA": meta, "RECDATA": recs})

    base = settings.ridb_base_url
    # Simulates unstable pagination: Upper Pines is missing from the state listing.
    respx.get(f"{base}/facilities", params={"state": "CA"}).mock(
        return_value=page("Lower Pines Campground", "North Pines Campground")
    )
    respx.get(f"{base}/facilities", params={"state": "NV"}).mock(return_value=page())
    respx.get(f"{base}/facilities", params={"radius": "54.0"}).mock(
        return_value=page("Upper Pines Campground", "Lower Pines Campground")
    )
    respx.get(url__regex=rf"{base}/recareas/\d+/facilities").mock(
        return_value=page("Bridalveil Creek Horse Campsites", "Upper Pines Campground")
    )

    stats = ingest_source(db, LocalStore(settings.raw_store_path), settings, SIERRA, "ridb")

    assert stats["found_by_state"] == 2
    assert stats["found_by_radius"] == 1
    assert stats["unlocated_by_recarea"] == 1
    rows = {r.name: r for r in _rows(db, "ridb").values()}
    assert set(rows) == {
        "Upper Pines Campground",
        "Lower Pines Campground",
        "North Pines Campground",
        "Bridalveil Creek Horse Campsites",
    }
    assert rows["Bridalveil Creek Horse Campsites"].geom is None
    # API keys are sent as headers and never stored with the request.
    with db.connect() as conn:
        stored = conn.execute(text("SELECT request FROM raw_objects")).scalars().all()
    assert all(settings.ridb_api_key.get_secret_value() not in json.dumps(r) for r in stored)  # type: ignore[union-attr]
