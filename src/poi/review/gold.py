"""ER gold set: a stratified sample of record pairs with *suggested* labels for a human.

Pairs are stored by (source, source_id), so labels survive database rebuilds. Suggested
labels come from the current model and are marked `verified: false`; metrics only ever use
labels a person verified with `poi review label`.

Strata (a pair can be in several):
- score bins over all candidate pairs (so precision/recall are measured across the range);
- hard cases, oversampled: close but differently named (< 300 m, name similarity < 70),
  same name far apart (> 5 km), shared phone number, and similar names that blocking did
  NOT pair (to measure blocking recall).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from poi.settings import PROJECT_ROOT

GOLD_PATH = PROJECT_ROOT / "eval" / "gold" / "er_pairs.jsonl"
SCORE_BINS = ((-1e9, -5.0), (-5.0, 0.0), (0.0, 4.0), (4.0, 8.0), (8.0, 1e9))


@dataclass
class GoldPair:
    a: dict[str, str]
    b: dict[str, str]
    strata: list[str]
    suggested: str | None = None
    suggestion_score: float | None = None  # None: the pair was not a blocking candidate
    # The gold label: drafted by Claude in a blind pass (no matcher score or decision shown).
    label: str | None = None  # match | non_match | unsure
    labeled_by: str | None = None  # "claude"
    rationale: str | None = None  # one line: shared ids, distance, discriminator words
    labeled_at: str | None = None
    note: str | None = None
    # Human verification of a random sample, done without seeing label or rationale.
    human_sample: bool = False
    human_label: str | None = None
    human_labeled_at: str | None = None
    human_note: str | None = None
    verified: bool = False  # True once a person labelled the pair

    @property
    def key(self) -> tuple[str, str, str, str]:
        ends = sorted(
            [(self.a["source"], self.a["source_id"]), (self.b["source"], self.b["source_id"])]
        )
        return (*ends[0], *ends[1])

    def to_json(self) -> str:
        data = {k: v for k, v in self.__dict__.items() if v is not None}
        data["verified"] = self.verified
        return json.dumps(data, ensure_ascii=False, sort_keys=True)


def load(path: Path = GOLD_PATH) -> list[GoldPair]:
    if not path.exists():
        return []
    return [GoldPair(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def save(pairs: list[GoldPair], path: Path = GOLD_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(p.to_json() + "\n" for p in pairs))
    tmp.replace(path)


PAIRS_SQL = text(
    """
    WITH r AS (
        SELECT id, source, source_id, name, match_name, phone, coalesce(shape, geom) AS g
        FROM poi_records WHERE region = :region AND is_campground
    )
    SELECT a.source AS a_source, a.source_id AS a_sid, a.name AS a_name,
           b.source AS b_source, b.source_id AS b_sid, b.name AS b_name,
           d.score, d.decision,
           CASE WHEN a.g IS NOT NULL AND b.g IS NOT NULL
                THEN ST_Distance(a.g::geography, b.g::geography) END AS distance_m,
           similarity(a.match_name, b.match_name) AS trgm,
           a.phone IS NOT NULL AND a.phone = b.phone AS same_phone,
           d.a_id IS NOT NULL AS is_candidate
    FROM r a JOIN r b ON a.id < b.id
    LEFT JOIN match_decisions d ON d.a_id = a.id AND d.b_id = b.id
    WHERE d.a_id IS NOT NULL
       OR (a.phone IS NOT NULL AND a.phone = b.phone)
       OR (a.match_name IS NOT NULL AND b.match_name IS NOT NULL
           AND similarity(a.match_name, b.match_name) >= 0.4)
    """
)


def _strata(row: Any) -> list[str]:
    out: list[str] = []
    if row.score is not None:
        for lo, hi in SCORE_BINS:
            if lo <= row.score < hi:
                out.append(
                    f"score[{lo:g},{hi:g})".replace("-1e+09", "-inf").replace("1e+09", "inf")
                )
    name_sim = (row.trgm or 0) * 100
    if (
        row.distance_m is not None
        and row.distance_m < 300
        and row.a_name
        and row.b_name
        and name_sim < 70
    ):
        out.append("hard:close_different_names")
    if row.distance_m is not None and row.distance_m > 5000 and name_sim >= 80:
        out.append("hard:same_name_far")
    if row.same_phone:
        out.append("hard:shared_phone")
    if not row.is_candidate and name_sim >= 60:
        out.append("hard:not_blocked_similar_name")
    return out


def suggest(conn: Connection, region: str, target: int = 200, seed: int = 7) -> list[GoldPair]:
    """Sample about `target` pairs: hard strata first (capped), then score bins evenly."""
    rows = [r for r in conn.execute(PAIRS_SQL, {"region": region}) if _strata(r)]
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not security
    rng.shuffle(rows)
    caps = {"hard:close_different_names": 40, "hard:same_name_far": 30,
            "hard:shared_phone": 25, "hard:not_blocked_similar_name": 20}  # fmt: skip
    chosen: dict[tuple[str, str, str, str], Any] = {}

    def add(row: Any) -> None:
        key = (row.a_source, row.a_sid, row.b_source, row.b_sid)
        chosen.setdefault(key, row)

    for stratum, cap in caps.items():
        for row in [r for r in rows if stratum in _strata(r)][:cap]:
            add(row)
    bins = [s for s in {x for r in rows for x in _strata(r)} if s.startswith("score")]
    per_bin = max(1, (target - len(chosen)) // max(len(bins), 1))
    for b in sorted(bins):
        for row in [r for r in rows if b in _strata(r)][:per_bin]:
            add(row)
    for row in rows:  # top up to the target from whatever is left
        if len(chosen) >= target:
            break
        add(row)

    def suggestion(row: Any) -> str:
        return "match" if row.decision == "match" else "non_match"

    return [
        GoldPair(
            a={"source": r.a_source, "source_id": r.a_sid, "name": r.a_name or ""},
            b={"source": r.b_source, "source_id": r.b_sid, "name": r.b_name or ""},
            strata=_strata(r),
            suggested=suggestion(r),
            suggestion_score=round(r.score, 2) if r.score is not None else None,
        )
        for r in chosen.values()
    ]


def merge(existing: list[GoldPair], new: list[GoldPair]) -> list[GoldPair]:
    """Keep every existing pair (and its verified label); add only pairs not yet present."""
    known = {p.key for p in existing}
    return existing + [p for p in new if p.key not in known]


def mark_verified(pair: GoldPair, label: str, note: str | None = None) -> None:
    """A person's label. Stored next to (never over) the drafted label, so agreement can be
    measured."""
    pair.human_label, pair.human_note, pair.verified = label, note, True
    pair.human_labeled_at = datetime.now(UTC).isoformat(timespec="seconds")


def mark_drafted(pair: GoldPair, label: str, rationale: str, by: str) -> None:
    pair.label, pair.rationale, pair.labeled_by = label, rationale, by
    pair.labeled_at = datetime.now(UTC).isoformat(timespec="seconds")


def draw_human_sample(pairs: list[GoldPair], n: int, seed: int) -> list[GoldPair]:
    """A uniform random sample (fixed seed, reproducible) for human verification."""
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not security
    chosen = rng.sample(pairs, min(n, len(pairs)))
    for pair in pairs:
        pair.human_sample = pair in chosen
    return chosen
