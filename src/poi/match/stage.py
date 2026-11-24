"""`poi match`: block, score, cluster and relate the region's campground records.

Idempotent: the region's previous ER rows are replaced. Results are explainable: every pair
keeps its feature contributions, and constraint-blocked edges become review items.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import Connection, Engine, text

from poi.log import get_logger
from poi.match import embeddings
from poi.match.blocking import candidate_rows, pair_geometry
from poi.match.cluster import cluster, hierarchy
from poi.match.scoring import (
    Candidate,
    Config,
    Frequencies,
    MatchRecord,
    Scored,
    load_config,
    score_pair,
)
from poi.normalize.names import match_key
from poi.resolve import overrides
from poi.runs import track_run
from poi.settings import Region

log = get_logger(__name__)
TABLES = ("record_relations", "cluster_members", "match_decisions", "candidate_pairs")


def load_records(conn: Connection, region: str) -> dict[int, MatchRecord]:
    rows = conn.execute(
        text(
            "SELECT id, source, source_id, name, match_name, alt_names, category, "
            "ST_Y(geom) AS lat, ST_X(geom) AS lon, precision_m, ids, phone, website, "
            "shape IS NOT NULL AS has_shape "
            "FROM poi_records WHERE region = :r AND is_campground"
        ),
        {"r": region},
    )
    return {
        r.id: MatchRecord(
            id=r.id,
            source=r.source,
            source_id=r.source_id,
            name=r.name,
            match_name=r.match_name,
            category=r.category,
            lat=r.lat,
            lon=r.lon,
            precision_m=r.precision_m,
            ids=r.ids,
            phone=r.phone,
            website=r.website,
            alt_match_names=tuple(k for n in r.alt_names if (k := match_key(n))),
            has_shape=r.has_shape,
        )
        for r in rows
    }


@dataclass
class CandidateRow:
    a_id: int
    b_id: int
    blocked_by: tuple[str, ...]
    distance_m: float | None
    a_contains_b: bool
    b_contains_a: bool


def _row(r: Any, blocked_by: tuple[str, ...]) -> CandidateRow:
    return CandidateRow(r.a_id, r.b_id, blocked_by, r.distance_m, r.a_contains_b, r.b_contains_a)


def add_embedding_candidates(
    conn: Connection,
    records: dict[int, MatchRecord],
    rows: list[CandidateRow],
    ecfg: dict[str, Any],
    embedder: embeddings.Embedder,
    use_as_feature: bool,
) -> tuple[list[CandidateRow], dict[tuple[int, int], float], dict[str, Any]]:
    """Union the embedding neighbours into the blocking candidates; similarities per pair
    when the feature is on."""
    vecs = embeddings.vectors(records.values(), embedder)
    proposed = embeddings.neighbour_pairs(vecs, ecfg["top_k"], ecfg["min_similarity"])
    by_key = {(r.a_id, r.b_id): r for r in rows}
    known = [k for k in proposed if k in by_key]
    for key in known:
        by_key[key].blocked_by = (*by_key[key].blocked_by, "embedding")
    too_far = 0
    for g in pair_geometry(conn, [k for k in proposed if k not in by_key]):
        if g.distance_m is not None and g.distance_m > ecfg["max_distance_m"]:
            too_far += 1
            continue
        by_key[(g.a_id, g.b_id)] = _row(g, ("embedding",))
    sims: dict[tuple[int, int], float] = {}
    if use_as_feature:
        for key in by_key:
            if (sim := embeddings.similarity(vecs, *key)) is not None:
                sims[key] = sim
    stats = {
        **embeddings.settings_summary(ecfg, embedder) | {"feature": use_as_feature},
        "embedded_records": len(vecs),
        "proposed_pairs": len(proposed),
        "already_candidates": len(known),
        "beyond_max_distance": too_far,
        "added_pairs": len(by_key) - len(rows),
    }
    return list(by_key.values()), sims, stats


def score_candidates(
    rows: list[CandidateRow],
    records: dict[int, MatchRecord],
    cfg: Config,
    freq: Frequencies,
    similarities: dict[tuple[int, int], float] | None = None,
) -> list[Scored]:
    sims = similarities or {}
    return [
        score_pair(
            Candidate(
                records[row.a_id],
                records[row.b_id],
                row.distance_m,
                tuple(row.blocked_by),
                row.a_contains_b,
                row.b_contains_a,
                sims.get((row.a_id, row.b_id)),
            ),
            freq,
            cfg,
        )
        for row in rows
    ]


def apply_overrides(
    scored: list[Scored], records: dict[int, MatchRecord], freq: Frequencies, cfg: Config
) -> tuple[list[Scored], frozenset[frozenset[int]]]:
    """Manual pair decisions from data/overrides.yaml replace the model's decision. A pair
    blocking never generated is scored on the spot, so the decision still has features."""
    by_ref = {f"{r.source}:{r.source_id}": r for r in records.values()}
    decided: dict[frozenset[int], str] = {}
    for m in overrides.load().match:
        if m.a in by_ref and m.b in by_ref:
            decided[frozenset((by_ref[m.a].id, by_ref[m.b].id))] = m.decision
    out = []
    for s in scored:
        key = frozenset((s.candidate.a.id, s.candidate.b.id))
        if key in decided:
            out.append(replace(s, decision=decided.pop(key), reason="override"))
        else:
            out.append(s)
    for key, decision in decided.items():  # not a blocking candidate
        a, b = sorted((records[i] for i in key), key=lambda r: r.id)
        s = score_pair(Candidate(a, b, None, ("override",)), freq, cfg)
        out.append(replace(s, decision=decision, reason="override"))
    non_matches = frozenset(
        frozenset((s.candidate.a.id, s.candidate.b.id))
        for s in out
        if s.reason == "override" and s.decision == "non_match"
    )
    return out, non_matches


def _insert(conn: Connection, sql: str, rows: list[dict[str, Any]]) -> None:
    if rows:
        conn.execute(text(sql), rows)


def match_region(
    engine: Engine,
    region: Region,
    use_embeddings: bool | None = None,
    embedding_feature: bool | None = None,
    embedder: embeddings.Embedder | None = None,
) -> dict[str, Any]:
    """`use_embeddings` / `embedding_feature` override config/matching.yaml (None = config)."""
    cfg = load_config()
    ecfg = cfg["embeddings"]
    use_embeddings = ecfg["enabled"] if use_embeddings is None else use_embeddings
    embedding_feature = ecfg["use_as_feature"] if embedding_feature is None else embedding_feature
    with track_run(engine, "match", region.name) as run, engine.begin() as conn:
        records = load_records(conn, region.name)
        freq = Frequencies(records.values())
        rows = [
            _row(r, tuple(r.blocked_by)) for r in candidate_rows(conn, region.name, cfg["blocking"])
        ]
        similarities: dict[tuple[int, int], float] = {}
        if use_embeddings:
            embedder = embedder or embeddings.FastEmbedder(ecfg["model"])
            rows, similarities, run.stats["embeddings"] = add_embedding_candidates(
                conn, records, rows, ecfg, embedder, embedding_feature
            )
        scored = score_candidates(rows, records, cfg, freq, similarities)
        scored, manual_non_match = apply_overrides(scored, records, freq, cfg)
        result = cluster(
            records,
            scored,
            cfg["cluster"]["max_diameter_m"],
            cfg["cluster"]["strong_non_match"],
            manual_non_match,
        )
        relations = hierarchy(scored, result.clusters)

        # Accepted edges the constraints rejected are not merges: send them to review.
        blocked = {id(b.scored): b.reason for b in result.blocked}
        final = [
            replace(s, decision="review", reason=f"constraint:{blocked[id(s)]}")
            if id(s) in blocked
            else s
            for s in scored
        ]

        for table in TABLES:
            conn.execute(text(f"DELETE FROM {table} WHERE region = :r"), {"r": region.name})  # noqa: S608
        base = {"run_id": run.id, "region": region.name}
        _insert(
            conn,
            "INSERT INTO candidate_pairs (run_id, region, a_id, b_id, distance_m, blocked_by, "
            "a_contains_b, b_contains_a) VALUES (:run_id, :region, :a, :b, :d, :by, :ab, :ba)",
            [
                base | {"a": s.candidate.a.id, "b": s.candidate.b.id,
                        "d": s.candidate.distance_m, "by": list(s.candidate.blocked_by),
                        "ab": s.candidate.a_contains_b, "ba": s.candidate.b_contains_a}
                for s in final
            ],
        )  # fmt: skip
        _insert(
            conn,
            "INSERT INTO match_decisions (run_id, region, a_id, b_id, score, contributions, "
            "evidence, decision, reason) VALUES (:run_id, :region, :a, :b, :score, "
            "CAST(:contrib AS jsonb), CAST(:evidence AS jsonb), :decision, :reason)",
            [
                base | {"a": s.candidate.a.id, "b": s.candidate.b.id, "score": s.score,
                        "contrib": json.dumps(s.contributions),
                        "evidence": json.dumps(s.evidence), "decision": s.decision,
                        "reason": s.reason}
                for s in final
            ],
        )  # fmt: skip
        _insert(
            conn,
            "INSERT INTO cluster_members (run_id, region, cluster_key, poi_record_id) "
            "VALUES (:run_id, :region, :key, :rid)",
            [
                base | {"key": key, "rid": rid}
                for key, ids in result.clusters.items()
                for rid in ids
            ],
        )
        _insert(
            conn,
            "INSERT INTO record_relations (run_id, region, child_id, parent_id, relation) "
            "VALUES (:run_id, :region, :child, :parent, :rel)",
            [base | {"child": r.child, "parent": r.parent, "rel": r.relation} for r in relations],
        )

        decisions = Counter(s.decision for s in final)
        sizes = Counter(len(m) for m in result.clusters.values())
        sources_per_cluster = Counter(
            len({records[i].source for i in m}) for m in result.clusters.values()
        )
        run.stats.update(
            records=len(records),
            candidate_pairs=len(final),
            decisions=dict(decisions),
            constraint_blocked=dict(Counter(b.reason for b in result.blocked)),
            clusters=len(result.clusters),
            merged_records=len(records) - len(result.clusters),
            cluster_sizes=dict(sorted(sizes.items())),
            clusters_by_source_count=dict(sorted(sources_per_cluster.items())),
            relations=dict(Counter(r.relation for r in relations)),
        )
        return dict(run.stats)
