"""`poi match` on the recorded fixtures: the Pines trio across three sources."""

from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import Engine, text

from conftest import load_fixture
from poi.ingest import ingest_source
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.settings import Settings, load_region
from poi.sources import ridb
from poi.storage.local import LocalStore

pytestmark = pytest.mark.integration
SIERRA = load_region("sierra")


@respx.mock
def test_pines_resolve_to_three_entities_across_sources(
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
    normalize_region(db, SIERRA, date(2026, 11, 24))

    first = match_region(db, SIERRA)
    second = match_region(db, SIERRA)
    assert first == second  # idempotent: previous ER rows replaced, same result

    with db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT cm.cluster_key, string_agg(p.source, ',' ORDER BY p.source) AS sources, "
                "min(p.name) AS name FROM cluster_members cm "
                "JOIN poi_records p ON p.id = cm.poi_record_id "
                "WHERE p.name ~ 'Pines' GROUP BY cm.cluster_key ORDER BY name"
            )
        ).all()
        decisions = conn.execute(text("SELECT count(*) FROM match_decisions")).scalar_one()
    assert [(r.name, r.sources) for r in rows] == [
        ("Lower Pines Campground", "nps,osm,ridb"),
        ("North Pines Campground", "nps,osm,ridb"),
        ("Upper Pines Campground", "nps,osm,ridb"),
    ]
    assert decisions == first["candidate_pairs"]
