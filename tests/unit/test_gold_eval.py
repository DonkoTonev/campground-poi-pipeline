from pathlib import Path

import pytest

from poi.eval.er import metrics, wilson
from poi.review import gold
from poi.review.label import map_link


def _pair(a: str, b: str, **kw: object) -> gold.GoldPair:
    fields: dict[str, object] = {"suggested": "match", "suggestion_score": 2.0} | kw
    return gold.GoldPair(
        a={"source": "osm", "source_id": a, "name": a},
        b={"source": "ridb", "source_id": b, "name": b},
        strata=["score[0,4)"],
        **fields,  # type: ignore[arg-type]
    )


def test_merge_keeps_verified_labels_and_adds_only_new_pairs(tmp_path: Path) -> None:
    labelled = _pair("1", "2")
    gold.mark_verified(labelled, "non_match", "different loops")
    merged = gold.merge([labelled], [_pair("1", "2"), _pair("3", "4")])
    assert len(merged) == 2
    assert merged[0].verified and merged[0].label == "non_match"
    path = tmp_path / "gold.jsonl"
    merged.append(_pair("5", "6", suggestion_score=None))  # not a blocking candidate
    gold.save(merged, path)
    again = gold.load(path)
    assert [p.key for p in again] == [p.key for p in merged]
    assert again[0].note == "different loops" and not again[1].verified


def test_pair_key_is_order_independent() -> None:
    a, b = _pair("1", "2"), _pair("1", "2")
    b.a, b.b = b.b, b.a
    assert a.key == b.key


def test_metrics_and_wilson_interval() -> None:
    m = metrics([True, True, False, False, True], [True, False, True, False, True])
    assert (m.tp, m.fp, m.fn, m.tn) == (2, 1, 1, 1)
    assert m.precision == pytest.approx(2 / 3) and m.recall == pytest.approx(2 / 3)
    lo, hi = wilson(45, 50)  # type: ignore[misc]
    assert 0.78 < lo < 0.9 < hi < 0.97
    assert wilson(0, 0) is None


def test_map_link_shows_both_points() -> None:
    a = {"lat": 37.7394, "lon": -119.5663}
    b = {"lat": 37.7361, "lon": -119.5631}
    assert map_link(a, b) == (
        "https://www.openstreetmap.org/directions?route=37.73940%2C-119.56630%3B37.73610%2C-119.56310"
    )
    assert map_link(a, {"lat": None, "lon": None}).startswith(  # type: ignore[union-attr]
        "https://www.openstreetmap.org/?mlat=37.73940"
    )
