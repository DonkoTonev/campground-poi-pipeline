"""`poi eval er`: precision / recall / F1 of entity resolution on *verified* gold labels.

Two views:
- clustering: a pair is predicted "match" when both records ended up in the same cluster
  (this is what the app shows: constraints and transitivity included);
- threshold sweep: pair scores alone, to see where the auto-merge threshold should sit.
Pairs blocking never generated count as predicted non-matches, so blocking misses show up
as lost recall. Intervals are Wilson 95%: with n in the low hundreds they are wide, and the
report says so instead of hiding it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from poi.review.gold import GoldPair, load
from poi.settings import PROJECT_ROOT, get_settings

REPORT_PATH = PROJECT_ROOT / "reports" / "er_eval.md"
THRESHOLDS = (-4.0, -2.0, 0.0, 2.0, 4.0, 6.0, 8.0)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Metrics:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "precision": self.precision, "recall": self.recall, "f1": self.f1,
            "precision_ci95": wilson(self.tp, self.tp + self.fp),
            "recall_ci95": wilson(self.tp, self.tp + self.fn),
        }  # fmt: skip


def metrics(truth: list[bool], predicted: list[bool]) -> Metrics:
    pairs = list(zip(truth, predicted, strict=True))
    return Metrics(
        tp=sum(t and p for t, p in pairs),
        fp=sum(p and not t for t, p in pairs),
        fn=sum(t and not p for t, p in pairs),
        tn=sum(not t and not p for t, p in pairs),
    )


@dataclass(frozen=True)
class Judged:
    pair: GoldPair
    truth: bool
    same_cluster: bool
    score: float | None
    contributions: dict[str, float]


def _ids(conn: Connection) -> dict[tuple[str, str], int]:
    rows = conn.execute(text("SELECT source, source_id, id FROM poi_records"))
    return {(r.source, r.source_id): r.id for r in rows}


def judge(conn: Connection, region: str) -> tuple[list[Judged], int]:
    """Join verified gold pairs with the latest clustering and pair scores."""
    ids = _ids(conn)
    cluster_of: dict[int, int] = dict(
        conn.execute(
            text("SELECT poi_record_id, cluster_key FROM cluster_members WHERE region = :r"),
            {"r": region},
        ).all()
    )
    scores = {
        (r.a_id, r.b_id): (r.score, r.contributions)
        for r in conn.execute(
            text("SELECT a_id, b_id, score, contributions FROM match_decisions WHERE region = :r"),
            {"r": region},
        )
    }
    judged: list[Judged] = []
    missing = 0
    for pair in load():
        if not pair.verified or pair.label not in ("match", "non_match"):
            continue
        a = ids.get((pair.a["source"], pair.a["source_id"]))
        b = ids.get((pair.b["source"], pair.b["source_id"]))
        if a is None or b is None or a not in cluster_of or b not in cluster_of:
            missing += 1  # record gone or no longer a campground: reported, not silently dropped
            continue
        score, contributions = scores.get((min(a, b), max(a, b)), (None, {}))
        judged.append(
            Judged(
                pair, pair.label == "match", cluster_of[a] == cluster_of[b], score, contributions
            )
        )
    return judged, missing


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def _ci(ci: tuple[float, float] | None) -> str:
    return "n/a" if ci is None else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def evaluate(conn: Connection, region: str) -> dict[str, Any]:
    judged, missing = judge(conn, region)
    truth = [j.truth for j in judged]
    clustering = metrics(truth, [j.same_cluster for j in judged])
    sweep = {
        t: metrics(truth, [j.score is not None and j.score >= t for j in judged])
        for t in THRESHOLDS
    }
    errors = {
        "false_merges": [j for j in judged if j.same_cluster and not j.truth],
        "missed_matches": [j for j in judged if j.truth and not j.same_cluster],
    }
    result = {
        "n_verified": len(judged),
        "n_missing_records": missing,
        "positives": sum(truth),
        "clustering": clustering.as_dict(),
        "threshold_sweep": {str(t): m.as_dict() for t, m in sweep.items()},
        "errors": {k: len(v) for k, v in errors.items()},
    }
    _write_report(region, result, clustering, sweep, errors)
    return result


def _config_hash() -> str:
    raw = (get_settings().config_dir / "matching.yaml").read_bytes()
    return hashlib.sha256(raw).hexdigest()[:10]


def _clustering_row(m: Metrics) -> str:
    p_ci, r_ci = wilson(m.tp, m.tp + m.fp), wilson(m.tp, m.tp + m.fn)
    return (
        f"| {_fmt(m.precision)} | {_ci(p_ci)} | {_fmt(m.recall)} | {_ci(r_ci)} | {_fmt(m.f1)} "
        f"| {m.tp} | {m.fp} | {m.fn} | {m.tn} |"
    )


def _write_report(
    region: str,
    result: dict[str, Any],
    clustering: Metrics,
    sweep: dict[float, Metrics],
    errors: dict[str, list[Judged]],
) -> None:
    lines = [
        "# Entity resolution evaluation",
        "",
        f"Region `{region}`, {date.today().isoformat()}, matching config `{_config_hash()}`.",
        f"Verified gold pairs: **{result['n_verified']}** ({result['positives']} matches). "
        f"Pairs whose records are missing or no longer campgrounds: {result['n_missing_records']}.",
        "",
        "Metrics use only labels a person verified (`poi review label`); suggested labels are",
        "never counted. Intervals are Wilson 95%.",
        "",
        "## Clustering (what the app shows)",
        "",
        "| precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |",
        "|---|---|---|---|---|---|---|---|---|",
        _clustering_row(clustering),
        "",
        "## Pair score threshold sweep (before clustering constraints)",
        "",
        "| threshold | precision | recall | F1 | FP | FN |",
        "|---|---|---|---|---|---|",
        *(
            f"| {t:g} | {_fmt(m.precision)} | {_fmt(m.recall)} | {_fmt(m.f1)} | {m.fp} | {m.fn} |"
            for t, m in sweep.items()
        ),
        "",
    ]
    for title, items in (("False merges", errors["false_merges"]),
                         ("Missed matches", errors["missed_matches"])):  # fmt: skip
        lines += [f"## {title} ({len(items)})", ""]
        for j in items[:15]:
            contrib = ", ".join(f"{k} {v:+g}" for k, v in sorted(
                j.contributions.items(), key=lambda kv: -abs(kv[1])))  # fmt: skip
            score = "not a candidate" if j.score is None else f"score {j.score:g}"
            lines.append(
                f"- {j.pair.a['source']}:{j.pair.a['name']} vs {j.pair.b['source']}:"
                f"{j.pair.b['name']} ({score}; {contrib or 'no features'})"
                + (f" note: {j.pair.note}" if j.pair.note else "")
            )
        lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines))


def summary_json(result: dict[str, Any]) -> str:
    return json.dumps(
        {k: result[k] for k in ("n_verified", "positives", "clustering", "errors")}, indent=2
    )
