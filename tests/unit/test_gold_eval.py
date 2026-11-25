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


def test_merge_keeps_labels_and_adds_only_new_pairs(tmp_path: Path) -> None:
    labelled = _pair("1", "2")
    gold.mark_drafted(labelled, "non_match", "Upper vs Lower, 80 m apart", "claude")
    gold.mark_verified(labelled, "non_match", "different loops")
    merged = gold.merge([labelled], [_pair("1", "2"), _pair("3", "4")])
    assert len(merged) == 2
    assert merged[0].label == "non_match" and merged[0].human_label == "non_match"
    merged.append(_pair("5", "6", suggestion_score=None))  # not a blocking candidate
    path = tmp_path / "gold.jsonl"
    gold.save(merged, path)
    again = gold.load(path)
    assert [p.key for p in again] == [p.key for p in merged]
    assert again[0].labeled_by == "claude" and again[0].rationale == "Upper vs Lower, 80 m apart"
    assert again[0].human_note == "different loops" and again[0].verified
    assert not again[1].verified


def test_human_label_never_overwrites_the_drafted_label() -> None:
    pair = _pair("1", "2")
    gold.mark_drafted(pair, "match", "same recreation.gov id", "claude")
    gold.mark_verified(pair, "non_match", None)
    assert pair.label == "match" and pair.human_label == "non_match"


def test_human_sample_is_reproducible_and_exact_size() -> None:
    pairs = [_pair(str(i), str(i + 1000)) for i in range(200)]
    first = {p.key for p in gold.draw_human_sample(pairs, 50, seed=7)}
    second = {p.key for p in gold.draw_human_sample(pairs, 50, seed=7)}
    assert first == second and len(first) == 50
    assert sum(p.human_sample for p in pairs) == 50


def test_agreement_statement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from poi.eval import er

    pairs = [_pair(str(i), str(i + 100)) for i in range(4)]
    for p, (drafted, human) in zip(
        pairs,
        [
            ("match", "match"),
            ("non_match", "non_match"),
            ("match", "non_match"),
            ("non_match", None),
        ],
        strict=True,
    ):
        gold.mark_drafted(p, drafted, "r", "claude")
        p.human_sample = True
        if human:
            gold.mark_verified(p, human)
    monkeypatch.setattr(er, "load", lambda: pairs)
    agr = er.agreement()
    assert (agr["human_verified"], agr["agree"], agr["agreement"]) == (3, 2, 0.667)
    assert er.label_statement(agr) == (
        "Claude-drafted labels (4 pairs, drafted blind to the matcher's score and decision), "
        "3 of 4 human-verified so far, agreement 67% (2/3) (provisional)."
    )
    assert len(agr["disagreements"]) == 1


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


def test_import_drafts_validates_before_changing_anything(tmp_path: Path) -> None:
    from poi.review import drafts

    pairs = [_pair("1", "2"), _pair("3", "4")]
    good = tmp_path / "labels_0.jsonl"
    good.write_text(
        '{"pair": 0, "label": "match", "rationale": "same recreation.gov id"}\n'
        '{"pair": 1, "label": "non_match", "rationale": "Upper vs Lower"}\n'
    )
    bad = tmp_path / "labels_1.jsonl"
    bad.write_text('{"pair": 1, "label": "maybe", "rationale": "x"}\n')
    with pytest.raises(ValueError, match="label 'maybe'"):
        drafts.import_drafts(pairs, [bad], "claude", force=False)
    assert all(p.label is None for p in pairs)
    assert drafts.import_drafts(pairs, [good], "claude", force=False) == 2
    assert (pairs[0].label, pairs[0].labeled_by) == ("match", "claude")
    with pytest.raises(ValueError, match="already have a drafted label"):
        drafts.import_drafts(pairs, [good], "claude", force=False)


def test_draw_sample_never_redraws() -> None:
    from poi.review import drafts

    pairs = [_pair(str(i), str(i + 100)) for i in range(10)]
    assert len(drafts.draw_sample(pairs, 4, seed=1)) == 4
    with pytest.raises(ValueError, match="already drawn"):
        drafts.draw_sample(pairs, 4, seed=2)
