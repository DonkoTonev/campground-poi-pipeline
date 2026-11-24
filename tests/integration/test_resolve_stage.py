"""Golden records end to end: stable ids through a split and a merge, overrides, redirects."""

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, text

from conftest import load_fixture
from poi.ingest import ingest_source
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.resolve import overrides
from poi.resolve.stage import resolve_region
from poi.settings import Settings, load_region
from poi.sources import ridb
from poi.storage.local import LocalStore

pytestmark = pytest.mark.integration
SIERRA = load_region("sierra")


def _entity_of(db: Engine, ref: str) -> tuple[str, str, list[str]]:
    with db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT e.id::text AS id, e.status, e.sources FROM entity_members m "
                "JOIN entities e ON e.id = m.entity_id WHERE m.record_ref = :ref"
            ),
            {"ref": ref},
        ).one()
    return row.id, row.status, sorted(row.sources)


def _refs(db: Engine, name: str) -> dict[str, str]:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT source, source_id FROM poi_records WHERE name = :n"), {"n": name}
        ).all()
    return {r.source: f"{r.source}:{r.source_id}" for r in rows}


def _rerun(db: Engine) -> dict[str, object]:
    match_region(db, SIERRA)
    return resolve_region(db, SIERRA)


@respx.mock
def test_entity_lifecycle(
    db: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ridb, "POLITE_DELAY_S", 0)
    monkeypatch.setattr(overrides, "OVERRIDES_PATH", tmp_path / "overrides.yaml")
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

    first = _rerun(db)
    refs = _refs(db, "Upper Pines Campground")
    upper_id, status, sources = _entity_of(db, refs["nps"])
    assert status == "active" and sources == ["nps", "osm", "ridb"]
    assert first["changes"] == {"created": first["entities"]}
    with db.connect() as conn:
        geometry_source = conn.execute(
            text(
                "SELECT source FROM entity_fields WHERE entity_id = CAST(:e AS uuid) "
                "AND field = 'geometry'"
            ),
            {"e": upper_id},
        ).scalar_one()
    assert geometry_source == "osm"  # mapped polygon beats agency points

    # Idempotent: nothing changes on a re-run.
    assert _rerun(db)["changes"] == {}

    # A reviewer says the OSM polygon is not Upper Pines: split. The id stays on the
    # larger part (NPS + RIDB), the OSM record gets a new entity.
    ovr = overrides.load()
    overrides.add_match(ovr, refs["osm"], refs["nps"], "non_match", "test split")
    overrides.add_match(ovr, refs["osm"], refs["ridb"], "non_match", "test split")
    overrides.save(ovr)
    split = _rerun(db)
    assert _entity_of(db, refs["nps"])[0] == upper_id
    osm_id, _, osm_sources = _entity_of(db, refs["osm"])
    assert osm_id != upper_id and osm_sources == ["osm"]
    assert split["changes"].get("created") == 1

    # The decision is reverted: merge back. The temporary entity redirects to the survivor.
    overrides.save(overrides.Overrides([], []))
    merged = _rerun(db)
    assert merged["changes"].get("merged") == 1
    assert _entity_of(db, refs["osm"])[0] == upper_id
    with db.connect() as conn:
        status, target = conn.execute(
            text(
                "SELECT e.status, r.new_id::text FROM entities e "
                "JOIN entity_redirects r ON r.old_id = e.id WHERE e.id = CAST(:e AS uuid)"
            ),
            {"e": osm_id},
        ).one()
    assert status == "merged" and target == upper_id

    # A field override wins, keeps the previous value as an alternative.
    ovr = overrides.load()
    overrides.add_field(ovr, refs["nps"], "phone", "+12095550199", "phone number changed")
    overrides.add_field(ovr, refs["nps"], "amenities.showers", "yes", "showers added")
    overrides.save(ovr)
    _rerun(db)
    with db.connect() as conn:
        golden = conn.execute(
            text("SELECT golden FROM entities WHERE id = CAST(:e AS uuid)"), {"e": upper_id}
        ).scalar_one()
        phone_source = conn.execute(
            text(
                "SELECT source FROM entity_fields WHERE entity_id = CAST(:e AS uuid) "
                "AND field = 'phone'"
            ),
            {"e": upper_id},
        ).scalar_one()
    assert golden["phone"] == "+12095550199" and phone_source == "override"
    assert golden["amenities"]["showers"] == "yes"
    assert "amenities.showers" not in golden
