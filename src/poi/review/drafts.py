"""Blind label drafting for the ER gold set, and the random sample a person verifies.

1. `poi review export-blind DIR`: each gold pair as the two records plus their distance, with
   no matcher score, decision, strata or suggestion, split into a few JSONL files.
2. A labeller that sees only those files (here: fresh Claude sessions with no access to the
   matcher's output) writes one line per pair: `{"pair": i, "label": ..., "rationale": ...}`.
3. `poi review import-drafts FILE... --by claude` stores the labels as drafted labels.
4. `poi review draw-sample --n 50 --seed S` picks the random pairs a person verifies with
   `poi review label`, which hides the drafted label and rationale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from poi.review.gold import GoldPair, draw_human_sample, mark_drafted

LABELS = ("match", "non_match", "unsure")
DISTANCE_SQL = text(
    """
    SELECT ST_Distance(coalesce(a.shape, a.geom)::geography, coalesce(b.shape, b.geom)::geography)
    FROM poi_records a, poi_records b
    WHERE a.source = :sa AND a.source_id = :ia AND b.source = :sb AND b.source_id = :ib
    """
)


def blind_view(conn: Connection, index: int, pair: GoldPair) -> dict[str, Any]:
    distance = conn.execute(
        DISTANCE_SQL,
        {"sa": pair.a["source"], "ia": pair.a["source_id"],
         "sb": pair.b["source"], "ib": pair.b["source_id"]},
    ).scalar()  # fmt: skip
    return {
        "pair": index,
        "a": pair.a,
        "b": pair.b,
        "distance_m": None if distance is None else round(float(distance), 1),
    }


def export_blind(conn: Connection, pairs: list[GoldPair], out_dir: Path, parts: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    views = [blind_view(conn, i, p) for i, p in enumerate(pairs)]
    size = -(-len(views) // parts)
    paths = []
    for n in range(parts):
        path = out_dir / f"pairs_{n}.jsonl"
        chunk = views[n * size : (n + 1) * size]
        path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in chunk))
        paths.append(path)
    return paths


def import_drafts(pairs: list[GoldPair], files: list[Path], by: str, force: bool) -> int:
    """Store drafted labels by pair index. Validates everything before changing anything."""
    drafts: dict[int, dict[str, Any]] = {}
    for path in files:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            i = int(row["pair"])
            if not 0 <= i < len(pairs):
                raise ValueError(f"{path}: pair {i} out of range")
            if row["label"] not in LABELS:
                raise ValueError(f"{path}: pair {i} has label {row['label']!r}")
            if not str(row.get("rationale", "")).strip():
                raise ValueError(f"{path}: pair {i} has no rationale")
            if i in drafts:
                raise ValueError(f"{path}: pair {i} labelled twice")
            drafts[i] = row
    already = [i for i in drafts if pairs[i].label is not None]
    if already and not force:
        raise ValueError(f"{len(already)} pairs already have a drafted label (use --force)")
    for i, row in drafts.items():
        mark_drafted(pairs[i], row["label"], row["rationale"].strip(), by)
    return len(drafts)


def draw_sample(pairs: list[GoldPair], n: int, seed: int) -> list[GoldPair]:
    if any(p.human_sample for p in pairs):
        raise ValueError("a human sample is already drawn; redrawing would discard it")
    return draw_human_sample(pairs, n, seed)
