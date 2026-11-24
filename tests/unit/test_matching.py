"""Matching edge cases from the brief (§10 "Matching"), on synthetic records.

Coordinates are around Yosemite Valley; distances are passed explicitly, as blocking
computes them in PostGIS.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from poi.match.cluster import cluster, hierarchy
from poi.match.scoring import (
    Candidate,
    Frequencies,
    MatchRecord,
    Scored,
    cannot_link,
    load_config,
    score_pair,
)
from poi.normalize.names import match_key

CFG = load_config()
_ids = iter(range(1, 10_000))


def rec(name: str | None, source: str = "osm", category: str = "developed_campground",
        lat: float = 37.74, lon: float = -119.56, **kw: Any) -> MatchRecord:  # fmt: skip
    rid = next(_ids)
    return MatchRecord(
        id=rid,
        source=source,
        source_id=kw.pop("source_id", f"{source}-{rid}"),
        name=name,
        match_name=match_key(name),
        category=category,
        lat=lat,
        lon=lon,
        alt_match_names=tuple(match_key(n) or "" for n in kw.pop("alt_names", ())),
        **kw,
    )


def score(a: MatchRecord, b: MatchRecord, distance_m: float | None, others: int = 300,
          **kw: Any) -> Scored:  # fmt: skip
    freq = Frequencies([a, b] + [rec(f"filler {i}") for i in range(others)])
    return score_pair(Candidate(a, b, distance_m, **kw), freq, CFG)


def test_distinct_campgrounds_within_200m_are_not_merged() -> None:
    upper = rec("Upper Pines Campground")
    lower = rec("Lower Pines Campground")
    s = score(upper, lower, 150)
    assert s.decision != "match"
    assert s.evidence["discriminator_conflict"] == ["position"]
    assert cannot_link(upper, lower) == "discriminator_conflict"


def test_same_campground_600m_apart_across_sources_is_merged() -> None:
    s = score(rec("Crane Flat Campground"), rec("CRANE FLAT", source="ridb"), 600)
    assert s.decision == "match", s.contributions


def test_same_name_far_apart_is_not_merged() -> None:
    mammoth = rec("Twin Lakes Campground", lat=37.62, lon=-119.01)
    bridgeport = rec("Lower Twin Lakes", source="ridb", lat=38.17, lon=-119.33)
    assert score(mammoth, bridgeport, 67_000).decision == "non_match"
    assert score(mammoth, rec("Twin Lakes", source="ridb"), 30_000).decision == "non_match"


@pytest.mark.parametrize(
    "other", ["TUOLUMNE MEADOWS", "Tuolumne Meadows Campground", "Tuolumne Meadows Cmpgd"]
)
def test_name_variants_are_merged(other: str) -> None:
    assert score(rec("Tuolumne Meadows CG"), rec(other, source="ridb"), 120).decision == "match"


def test_group_or_horse_camp_next_to_family_campground_is_separate() -> None:
    family = rec("Tuolumne Meadows Campground")
    horse = rec("Tuolumne Meadows Horse Camp", source="nps", category="horse")
    group = rec("Tuolumne Meadows Group Campground", category="group")
    for other in (horse, group):
        s = score(family, other, 80)
        assert s.decision == "non_match", s.contributions
        assert cannot_link(family, other) == "discriminator_conflict"


def test_group_site_inside_campground_polygon_is_a_child_not_a_merge() -> None:
    camp = rec("Agnew Meadows Campground", has_shape=True)
    group = rec("Agnew Meadows Group Campground", category="group")
    s = score(camp, group, 0, a_contains_b=True)
    result = cluster({camp.id: camp, group.id: group}, [s], 3000, -5)
    assert len(result.clusters) == 2
    rel = hierarchy([s], result.clusters)
    assert [(r.child, r.parent, r.relation) for r in rel] == [(group.id, camp.id, "sub_area")]


def test_unnamed_site_inside_polygon_becomes_child() -> None:
    camp = rec("Saddlebag Lake Campground", has_shape=True)
    pitch = rec(None)
    s = score(camp, pitch, 0, a_contains_b=True)
    assert s.decision != "match"
    rel = hierarchy([s], cluster({camp.id: camp, pitch.id: pitch}, [s], 3000, -5).clusters)
    assert [(r.child, r.relation) for r in rel] == [(pitch.id, "unnamed_part")]


def test_shared_ranger_district_phone_alone_is_not_enough() -> None:
    phone = "+17605550123"
    district = [rec(f"Campground {i}", phone=phone) for i in range(20)]
    a = rec("Big Bend Campground", phone=phone)
    b = rec("Oh Ridge Campground", source="ridb", phone=phone)
    freq = Frequencies([a, b, *district, *(rec(f"x {i}") for i in range(300))])
    s = score_pair(Candidate(a, b, 900), freq, CFG)
    assert s.decision == "non_match"
    assert s.contributions["phone"] < 1.0  # shared by 22 records: weak


def test_unique_phone_counts_far_more_than_a_shared_district_phone() -> None:
    phone = "+17605550100"
    a, b = rec("Pine Cliff Resort", phone=phone), rec("Pine Cliff RV", phone=phone)
    unique = score(a, b, 300).contributions["phone"]
    district = [rec(f"Campground {i}", phone=phone) for i in range(20)]
    freq = Frequencies([a, b, *district, *(rec(f"x {i}") for i in range(300))])
    shared = score_pair(Candidate(a, b, 300), freq, CFG).contributions["phone"]
    assert unique > 2.0 and shared < 1.0 and unique > 3 * shared


def test_shared_recreation_gov_id_with_different_names_is_matched() -> None:
    nps = rec("Camp 4 Campground", source="nps", ids={"recgov_facility_id": "10004152"})
    ridb = rec("Sunnyside Walk-in", source="ridb", ids={"recgov_facility_id": "10004152"})
    assert score(nps, ridb, 90).decision == "match"
    assert cannot_link(nps, ridb) is None  # shared id outweighs the walk-in discriminator


def test_osm_node_and_way_duplicate_is_merged() -> None:
    way = rec("Upper Pines Campground", source_id="way/1", has_shape=True)
    node = rec("Upper Pines Campground", source_id="node/2")
    s = score(way, node, 0, a_contains_b=True)
    assert s.decision == "match"
    assert len(cluster({way.id: way, node.id: node}, [s], 3000, -5).clusters) == 1


def test_two_ridb_facilities_100m_apart_cannot_link_even_with_overwhelming_score() -> None:
    a = rec("Robinson Creek", source="ridb", source_id="1001")
    b = rec("Robinson Creek", source="ridb", source_id="1002")
    s = score(a, b, 100)
    assert s.decision == "match"  # the score alone would merge them...
    result = cluster({a.id: a, b.id: b}, [s], 3000, -5)
    assert len(result.clusters) == 2  # ...the constraint keeps them apart
    assert [x.reason for x in result.blocked] == ["distinct_ridb_ids"]


def test_transitive_chain_is_split_on_the_weaker_edge() -> None:
    a = rec("Sawmill Campground", source="ridb", source_id="1")
    b = rec("Sawmill Campground", source="osm")
    c = rec("Sawmill Campground", source="ridb", source_id="2")
    strong = score(a, b, 20)
    weak = score(b, c, 900)
    assert strong.score > weak.score and weak.decision == "match"
    result = cluster({r.id: r for r in (a, b, c)}, [strong, weak], 3000, -5)
    assert sorted(result.clusters.values()) == [[a.id, b.id], [c.id]]
    assert result.blocked[0].scored is weak


def test_low_precision_coordinates_still_match_through_name_blocking() -> None:
    precise = rec("Honeymoon Flat Campground", lat=38.2013, lon=-119.3201)
    coarse = rec("HONEYMOON FLAT", source="ridb", lat=38.21, lon=-119.33, precision_m=1113)
    s = score(precise, coarse, 1350, blocked_by=("name",))
    assert s.decision == "match", s.contributions


def test_unlocated_record_matches_on_name_and_id() -> None:
    located = rec("Bridalveil Creek Horse Camp", source="nps", category="horse",
                  ids={"recgov_facility_id": "1"})  # fmt: skip
    unlocated = rec("Bridalveil Creek Horse Campsites", source="ridb", category="horse",
                    lat=None, lon=None, ids={"recgov_facility_id": "1"})  # type: ignore[arg-type]  # fmt: skip
    assert score(located, unlocated, None).decision == "match"


def test_renamed_campground_goes_to_review_with_old_name() -> None:
    osm = rec("AutoCamp Yosemite", alt_names=["Yosemite West / Mariposa KOA"])
    ridb = rec("Yosemite West Mariposa KOA", source="ridb")
    s = score(osm, ridb, 40)
    assert s.decision == "review" and s.reason == "possible_rename"


def test_every_decision_is_explained() -> None:
    s = score(rec("Upper Pines Campground"), rec("Lower Pines Campground"), 150)
    assert s.score == pytest.approx(sum(s.contributions.values()), abs=1e-3)
    assert {"prior", "distance", "name", "discriminator_conflict"} <= s.contributions.keys()


def test_shared_id_merges_despite_bad_coordinates_but_fuzzy_chains_respect_diameter() -> None:
    # Real case: RIDB's Crane Flat point is 4.2 km from the NPS/OSM points, same rec.gov id.
    nps = rec("Crane Flat Campground", source="nps", ids={"recgov_facility_id": "232452"})
    ridb = rec("Crane Flat Campground", source="ridb", lat=37.7639, lon=-119.8444,
               ids={"recgov_facility_id": "232452"})  # fmt: skip
    s = score(nps, ridb, 4196)
    assert s.decision == "match"
    assert len(cluster({nps.id: nps, ridb.id: ridb}, [s], 3000, -5).clusters) == 1

    # Without the id, the same far-apart edge would be blocked by the diameter guard.
    a, b = rec("Crane Flat Campground"), rec("Crane Flat Campground", lat=37.7639, lon=-119.8444)
    forced = replace(score(a, b, 4196), decision="match")
    result = cluster({a.id: a, b.id: b}, [forced], 3000, -5)
    assert len(result.clusters) == 2 and result.blocked[0].reason == "max_diameter"


def test_third_source_joins_cluster_whose_member_has_a_bad_point() -> None:
    # Order matters in real data: RIDB+NPS merge first (shared id, 4.2 km apart), then OSM
    # (next to NPS) must still join although it is 4 km from RIDB's wrong point.
    nps = rec("Crane Flat Campground", source="nps", ids={"recgov_facility_id": "232452"})
    ridb = rec("Crane Flat Campground", source="ridb", lat=37.7639, lon=-119.8444,
               ids={"recgov_facility_id": "232452"})  # fmt: skip
    osm = rec("Crane Flat Campground", lat=37.7400, lon=-119.5600)
    edges = [score(nps, ridb, 4196), score(osm, nps, 0)]
    result = cluster({r.id: r for r in (nps, ridb, osm)}, edges, 3000, -5)
    assert list(result.clusters.values()) == [sorted([nps.id, ridb.id, osm.id])]


def test_old_and_new_namesakes_are_different_campgrounds() -> None:
    s = score(rec("Old Shady Rest Campground"), rec("New Shady Rest Campground"), 30)
    assert s.decision == "non_match"


def test_unnamed_sites_near_each_other_do_not_auto_merge() -> None:
    assert score(rec(None), rec(None), 300).decision == "non_match"
