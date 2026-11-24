"""Golden-record building blocks: stable ids, survivorship, quality score, overrides."""

from collections.abc import Callable
from pathlib import Path

import pytest

from poi.resolve import ids, overrides, quality
from poi.resolve.survivorship import (
    Candidate,
    NameCandidate,
    PointCandidate,
    Rules,
    choose,
    choose_amenities,
    choose_geometry,
    choose_name,
)
from poi.settings import load_yaml

RULES = Rules(load_yaml("survivorship.yaml"))


def _counter() -> Callable[[], str]:
    n = iter(range(1, 1000))
    return lambda: f"new-{next(n)}"


# --- stable ids -----------------------------------------------------------------------


def test_unchanged_clusters_keep_their_ids() -> None:
    prev = {"E1": frozenset({"osm:1", "nps:a"}), "E2": frozenset({"ridb:9"})}
    out = ids.assign(prev, [frozenset({"ridb:9"}), frozenset({"osm:1", "nps:a"})], _counter())
    assert out.cluster_ids == {0: "E2", 1: "E1"}
    assert out.created == [] and out.redirects == {} and out.retired == []


def test_new_record_joining_keeps_id_and_new_cluster_gets_new_id() -> None:
    prev = {"E1": frozenset({"osm:1"})}
    out = ids.assign(prev, [frozenset({"osm:1", "ridb:2"}), frozenset({"osm:3"})], _counter())
    assert out.cluster_ids == {0: "E1", 1: "new-1"} and out.created == ["new-1"]


def test_merge_redirects_the_smaller_entity_to_the_survivor() -> None:
    prev = {"E1": frozenset({"osm:1", "nps:a"}), "E2": frozenset({"ridb:9"})}
    out = ids.assign(prev, [frozenset({"osm:1", "nps:a", "ridb:9"})], _counter())
    assert out.cluster_ids == {0: "E1"}
    assert out.redirects == {"E2": "E1"}


def test_split_keeps_id_on_largest_part_without_redirect() -> None:
    prev = {"E1": frozenset({"osm:1", "nps:a", "ridb:9"})}
    out = ids.assign(prev, [frozenset({"ridb:9"}), frozenset({"osm:1", "nps:a"})], _counter())
    assert out.cluster_ids == {0: "new-1", 1: "E1"}
    assert out.redirects == {} and out.created == ["new-1"]


def test_entity_whose_records_all_disappeared_is_retired_not_deleted() -> None:
    prev = {"E1": frozenset({"osm:1"}), "E2": frozenset({"osm:2"})}
    out = ids.assign(prev, [frozenset({"osm:1"})], _counter())
    assert out.retired == ["E2"]


# --- survivorship ---------------------------------------------------------------------


def test_highest_trust_wins_and_alternatives_keep_provenance() -> None:
    result = choose("phone", [Candidate("+12095550101", "osm", "osm:1"),
                              Candidate("+12095550102", "nps", "nps:a")], RULES)  # fmt: skip
    assert result is not None and result.value == "+12095550102" and result.source == "nps"
    assert result.alternatives == [{"value": "+12095550101", "source": "osm", "record": "osm:1"}]


def test_corroboration_can_beat_a_single_more_trusted_source() -> None:
    # reservable: ridb (1.0) alone vs nps (0.9) + osm agreeing (0.9 + 0.15 = 1.05)
    r = choose(
        "reservable",
        [Candidate(True, "ridb", "ridb:1"), Candidate(False, "nps", "nps:a"),
         Candidate(False, "osm", "osm:1")],
        RULES,
    )  # fmt: skip
    assert r is not None and r.value is False and "corroborated by nps, osm" in r.reason
    assert r.conflict  # the losing side (ridb) is trusted: high-impact conflict for review
    # phone: ridb (0.8) + osm agreeing (0.95) beats nps (0.9) alone
    r = choose(
        "phone",
        [Candidate("+1", "ridb", "ridb:1"), Candidate("+1", "osm", "osm:1"),
         Candidate("+2", "nps", "nps:a")],
        RULES,
    )  # fmt: skip
    assert r is not None and r.value == "+1"


def test_missing_values_never_win() -> None:
    assert choose("website", [Candidate(None, "nps", "nps:a")], RULES) is None


def test_name_vote_prefers_majority_and_keeps_variants_as_alt_names() -> None:
    result, alts = choose_name(
        [
            NameCandidate("Camp 4 Campground", "camp 4 campground", "nps", "nps:a"),
            NameCandidate("Camp 4", "camp 4", "osm", "osm:1", ("Sunnyside Walk-in Campground",)),
            NameCandidate("Camp 4", "camp 4", "ridb", "ridb:9"),
        ],
        RULES,
    )
    assert result is not None and result.value == "Camp 4"  # 0.8 + 0.6 > 1.0
    assert alts == ["Camp 4 Campground", "Sunnyside Walk-in Campground"]


def test_polygon_geometry_wins_and_far_points_are_a_conflict() -> None:
    result = choose_geometry(
        [
            PointCandidate(37.7639, -119.8444, False, "ridb", "ridb:232452"),  # 4 km off
            PointCandidate(37.7498, -119.8003, False, "nps", "nps:a"),
            PointCandidate(37.7464, -119.8001, True, "osm", "osm:way/1"),
        ],
        RULES,
    )
    assert result is not None and result.source == "osm" and result.reason == "polygon from osm"
    assert result.conflict


def test_unknown_amenity_never_overrides_a_stated_value() -> None:
    golden, results = choose_amenities(
        [("nps", "nps:a", {"showers": "unknown", "drinking_water": "yes"}),
         ("osm", "osm:1", {"showers": "no", "drinking_water": "unknown"})],
        RULES,
    )  # fmt: skip
    assert golden == {"drinking_water": "yes", "showers": "no"}
    assert set(results) == {"drinking_water", "showers"}


# --- quality ----------------------------------------------------------------------------


def test_quality_score_components() -> None:
    cfg = RULES.raw["quality"]
    full = {"name": "x", "geometry": {"lat": 1}, "category": "c", "phone": "p", "reservable": True,
            "season": {"months_open": [6]}, "num_sites": 5,
            "amenities": {"showers": "yes", "drinking_water": "yes", "laundry": "no"}}  # fmt: skip
    best = quality.score(quality.QualityInputs(full, 3, 0, False, 0, 0), cfg["weights"],
                         cfg["key_fields"])  # fmt: skip
    assert best["score"] == pytest.approx(1.0) and best["missing"] == []
    thin = quality.score(quality.QualityInputs({"name": "x"}, 1, 2, True, 1, 2), cfg["weights"],
                         cfg["key_fields"])  # fmt: skip
    assert thin["score"] < 0.4 and "season" in thin["missing"] and thin["freshness"] < 0.5


# --- overrides --------------------------------------------------------------------------


def test_overrides_roundtrip_and_latest_decision_wins(tmp_path: Path) -> None:
    path = tmp_path / "overrides.yaml"
    ovr = overrides.load(path)
    overrides.add_match(ovr, "osm:way/1", "ridb:2", "match", "same place on the map")
    overrides.add_match(ovr, "ridb:2", "osm:way/1", "non_match", "loops are separate")
    overrides.add_field(ovr, "nps:a", "amenities.dump_station", "no", "NPS page says no")
    overrides.save(ovr, path)
    again = overrides.load(path)
    assert [(m.decision, m.reason) for m in again.match] == [("non_match", "loops are separate")]
    assert again.fields[0].field == "amenities.dump_station" and again.fields[0].value == "no"
    assert path.read_text().startswith("# Manual decisions")
