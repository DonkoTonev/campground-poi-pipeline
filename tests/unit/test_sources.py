"""Source parsers on recorded, scrubbed fixtures (real OSM / RIDB / NPS records)."""

import copy
import json

from conftest import fixture_bytes, load_fixture
from poi.settings import load_region
from poi.sources import nps, osm, ridb

SIERRA = load_region("sierra")


def test_overpass_query_keeps_relation_members() -> None:
    q = osm.build_query(SIERRA.bbox)
    assert "out body geom;" in q  # `out tags` silently drops relation members
    assert '"tourism"~"^(camp_site|caravan_site)$"' in q
    assert "(37.3,-120.3,38.3,-118.8)" in q


def test_osm_ways_become_polygons_with_inner_representative_point() -> None:
    records = {r.name: r for r in osm.parse(fixture_bytes("osm/overpass.json"))}
    pines = [records[f"{p} Pines Campground"] for p in ("Upper", "Lower", "North")]
    for rec in pines:
        assert rec.source_id.startswith("way/")
        assert rec.shape is not None and rec.shape.geom_type == "Polygon"
        assert rec.point is not None and rec.shape.contains(rec.point)
    # The edge case the whole ER design is about: three distinct campgrounds within ~1 km.
    upper, lower, north = (r.point for r in pines)
    assert max(upper.distance(lower), lower.distance(north)) < 0.015  # degrees, ~1.3 km


def test_osm_multipolygon_relation_is_assembled() -> None:
    rec = next(
        r
        for r in osm.parse(fixture_bytes("osm/overpass.json"))
        if r.source_id.startswith("relation/")
    )
    assert rec.name == "Tuolumne Meadows Campground"
    assert rec.shape is not None and rec.shape.area > 0
    assert rec.shape.contains(rec.point)


def test_osm_nodes_are_points_without_shape() -> None:
    nodes = [
        r for r in osm.parse(fixture_bytes("osm/overpass.json")) if r.source_id.startswith("node/")
    ]
    assert nodes and all(r.point is not None and r.shape is None for r in nodes)


def test_osm_skips_non_campsite_tourism() -> None:
    data = load_fixture("osm/overpass.json")
    data["elements"][0]["tags"]["tourism"] = "camp_pitch"  # individual pitches are excluded
    ids = {r.source_id for r in osm.parse(json.dumps(data).encode())}
    assert f"{data['elements'][0]['type']}/{data['elements'][0]['id']}" not in ids


def test_ridb_keeps_only_campgrounds_inside_region() -> None:
    names = {r.name for r in ridb.parse(fixture_bytes("ridb/facilities.json"), SIERRA)}
    assert {"Upper Pines Campground", "Lower Pines Campground", "North Pines Campground"} <= names
    assert "FIR GROUP" not in names  # campground, but outside the bbox
    assert all("Trailhead" not in n for n in names if n)  # not a campground


def test_ridb_unlocated_campground_kept_only_via_home_recarea() -> None:
    data = load_fixture("ridb/facilities.json")
    lower = next(f for f in data["RECDATA"] if f["FacilityName"] == "Lower Pines Campground")
    unlocated = copy.deepcopy(lower)
    unlocated.update(FacilityID="999999", FacilityLatitude=0, FacilityLongitude=0)
    body = json.dumps({"RECDATA": [unlocated]}).encode()

    assert list(ridb.parse(body, SIERRA)) == []  # never placed in the region by bbox
    pending = [(7, f) for f in ridb.unlocated_campgrounds(body)]
    home = ridb.recarea_ids(lower)
    kept = list(ridb.resolve_unlocated(pending, home))
    assert len(kept) == 1 and kept[0].raw_object_id == 7
    assert kept[0].record.point is None
    assert kept[0].record.payload["_region_by"] == "recarea"
    assert list(ridb.resolve_unlocated(pending, {"some-other-recarea"})) == []


def test_ridb_drops_bulky_keys_and_reads_total() -> None:
    rec = next(ridb.parse(fixture_bytes("ridb/facilities.json"), SIERRA))
    assert not ridb.DROP_KEYS & rec.payload.keys()
    data = load_fixture("ridb/facilities.json")
    assert ridb.total_count(fixture_bytes("ridb/facilities.json")) == len(data["RECDATA"])


def test_ridb_radius_reaches_bbox_corners() -> None:
    assert 50 <= ridb.bbox_radius_miles(SIERRA.bbox) <= 60


def test_nps_parses_string_coordinates_and_horse_camp() -> None:
    records = {r.name: r for r in nps.parse(fixture_bytes("nps/campgrounds.json"))}
    assert "Tuolumne Meadows Horse Camp" in records and "Tuolumne Meadows Campground" in records
    assert all(r.point is not None for r in records.values())


def test_nps_empty_coordinates_become_none() -> None:
    data = load_fixture("nps/campgrounds.json")
    data["data"][0]["latitude"] = ""
    first = next(nps.parse(json.dumps(data).encode()))
    assert first.point is None
