"""`poi normalize` against PostGIS: idempotent, flags replaced not duplicated."""

from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import Engine, text

from conftest import load_fixture
from poi.ingest import ingest_source
from poi.normalize.stage import normalize_region
from poi.settings import Settings, load_region
from poi.sources import ridb
from poi.storage.local import LocalStore

pytestmark = pytest.mark.integration
SIERRA = load_region("sierra")


@respx.mock
def test_normalize_is_idempotent(
    db: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ridb, "POLITE_DELAY_S", 0)
    store = LocalStore(settings.raw_store_path)
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

    first = normalize_region(db, SIERRA, date(2026, 11, 24))
    second = normalize_region(db, SIERRA, date(2026, 11, 24))
    assert first["records"] == second["records"] > 0
    assert first["flags"] == second["flags"]

    with db.connect() as conn:
        n_records, n_sources = conn.execute(
            text("SELECT (SELECT count(*) FROM poi_records), (SELECT count(*) FROM source_records)")
        ).one()
        n_flags = conn.execute(text("SELECT count(*) FROM quality_flags")).scalar_one()
        shapes = conn.execute(
            text("SELECT count(*) FROM poi_records WHERE shape IS NOT NULL AND geom IS NOT NULL")
        ).scalar_one()
        unlocated = (
            conn.execute(
                text(
                    "SELECT f.rule FROM quality_flags f JOIN poi_records p ON p.id = f.subject_id "
                    "WHERE p.name = 'Bridalveil Creek Horse Campsites' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
    assert n_records == n_sources
    assert n_flags == sum(second["flags"].values())  # replaced, not appended
    assert shapes == 4  # OSM polygons carried over with their representative points
    # Real RIDB record without coordinates: kept, flagged, never silently dropped.
    assert {"null_island", "region_by_recarea"} <= set(unlocated)
