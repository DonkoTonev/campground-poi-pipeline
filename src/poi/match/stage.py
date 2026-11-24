"""`poi match`: block, score, cluster and relate the region's campground records.

Idempotent: the region's previous ER rows are replaced. Results are explainable: every pair
keeps its feature contributions, and constraint-blocked edges become review items.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from typing import Any

from sqlalchemy import Connection, Engine, text

from poi.log import get_logger
from poi.match.blocking import candidate_rows
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


def score_candidates(
    conn: Connection, region: str, records: dict[int, MatchRecord], cfg: Config
) -> list[Scored]:
    freq = Frequencies(records.values())
    return [
        score_pair(
            Candidate(
                records[row.a_id],
                records[row.b_id],
                row.distance_m,
                tuple(row.blocked_by),
                row.a_contains_b,
                row.b_contains_a,
            ),
            freq,
            cfg,
        )
        for row in candidate_rows(conn, region, cfg["blocking"])
    ]


def _insert(conn: Connection, sql: str, rows: list[dict[str, Any]]) -> None:
    if rows:
        conn.execute(text(sql), rows)


def match_region(engine: Engine, region: Region) -> dict[str, Any]:
    cfg = load_config()
    with track_run(engine, "match", region.name) as run, engine.begin() as conn:
        records = load_records(conn, region.name)
        scored = score_candidates(conn, region.name, records, cfg)
        result = cluster(
            records, scored, cfg["cluster"]["max_diameter_m"], cfg["cluster"]["strong_non_match"]
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
