"""Embedding candidates and feature with a deterministic fake model (no fastembed needed)."""

from __future__ import annotations

import numpy as np

from conftest import TrigramEmbedder
from poi.match import embeddings
from poi.match.scoring import Candidate, Frequencies, MatchRecord, load_config, score_pair
from poi.normalize.names import match_key

CFG = load_config()


def rec(rid: int, name: str | None) -> MatchRecord:
    return MatchRecord(rid, "osm", str(rid), name, match_key(name), "developed_campground")


def test_vectors_are_unit_length_and_skip_unnamed() -> None:
    vecs = embeddings.vectors([rec(1, "Upper Pines"), rec(2, None)], TrigramEmbedder())
    assert set(vecs) == {1}
    assert abs(float(np.linalg.norm(vecs[1])) - 1) < 1e-6


def test_neighbour_pairs_respect_top_k_and_threshold() -> None:
    records = [rec(1, "Agnew Meadows"), rec(2, "Agnew Meadow"), rec(3, "Silver Lake"),
               rec(4, "Silver Lakes"), rec(5, "Zzyzx")]  # fmt: skip
    vecs = embeddings.vectors(records, TrigramEmbedder())
    pairs = embeddings.neighbour_pairs(vecs, top_k=1, min_similarity=0.8)
    assert set(pairs) == {(1, 2), (3, 4)}  # ordered (low, high), dissimilar names left out
    assert all(0.8 <= s <= 1.0 for s in pairs.values())
    assert embeddings.neighbour_pairs({1: vecs[1]}, 5, 0.0) == {}


def test_feature_is_binned_and_explained() -> None:
    a, b = rec(1, "Upper Pines"), rec(2, "Upper Pines")
    freq = Frequencies([a, b])
    without = score_pair(Candidate(a, b, 30.0), freq, CFG)
    with_sim = score_pair(Candidate(a, b, 30.0, embedding_similarity=0.97), freq, CFG)
    assert "embedding" not in without.contributions
    top = CFG["embeddings"]["feature"][0]
    assert with_sim.contributions["embedding"] == top[1]
    assert with_sim.evidence["embedding_similarity"] == 0.97
    low = score_pair(Candidate(a, b, 30.0, embedding_similarity=0.1), freq, CFG)
    assert low.contributions["embedding"] == CFG["embeddings"]["feature"][-1][1]


def test_off_by_default() -> None:
    assert CFG["embeddings"]["enabled"] is False
