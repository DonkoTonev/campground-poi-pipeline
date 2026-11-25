"""`poi eval er`: precision / recall / F1 of entity resolution against the gold labels.

Truth is the Claude-drafted label (blind to the matcher) for every pair; the human-verified
sample is scored separately, with the agreement rate between the two.

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
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from poi.review.gold import GoldPair, disagrees, final_label, load
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
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "precision_ci95": wilson(self.tp, self.tp + self.fp),
            "recall_ci95": wilson(self.tp, self.tp + self.fn),
        }


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


def judge(conn: Connection, region: str, truth: str = "drafted") -> tuple[list[Judged], int]:
    """Join gold pairs with the latest clustering and pair scores. `truth` picks the label:
    drafted (Claude, blind, every pair), human (sample), final (sample: the agreed label or
    the adjudicated one) or corrected (every pair: drafted, replaced by the adjudicated
    label where there is one)."""
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
        label = {
            "drafted": pair.label,
            "human": pair.human_label,
            "final": final_label(pair),  # sample only: agreed or adjudicated
            "corrected": pair.final_label or pair.label,  # all pairs, adjudication applied
        }[truth]
        if label not in ("match", "non_match"):
            continue
        a = ids.get((pair.a["source"], pair.a["source_id"]))
        b = ids.get((pair.b["source"], pair.b["source_id"]))
        if a is None or b is None or a not in cluster_of or b not in cluster_of:
            missing += 1  # record gone or no longer a campground: reported, not silently dropped
            continue
        score, contributions = scores.get((min(a, b), max(a, b)), (None, {}))
        judged.append(
            Judged(pair, label == "match", cluster_of[a] == cluster_of[b], score, contributions)
        )
    return judged, missing


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def _ci(ci: tuple[float, float] | None) -> str:
    return "n/a" if ci is None else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def agreement() -> dict[str, Any]:
    """Human vs drafted labels on the verification sample."""
    pairs = load()
    sample = [p for p in pairs if p.human_sample]
    both = [p for p in sample if p.human_label and p.label]
    agree = [p for p in both if p.human_label == p.label]
    return {
        "drafted": sum(p.label is not None for p in pairs),
        "drafted_by": sorted({p.labeled_by for p in pairs if p.labeled_by}),
        "sample_size": len(sample),
        "human_verified": len(both),
        "agree": len(agree),
        "agreement": round(len(agree) / len(both), 3) if both else None,
        "disagreement_kinds": dict(
            Counter(f"{p.label} -> {p.human_label}" for p in both if p.human_label != p.label)
        ),
        "adjudicated": sum(p.final_label is not None for p in both if disagrees(p)),
        "final_sides_with_draft": sum(p.final_label == p.label for p in both if disagrees(p)),
        "final_sides_with_human": sum(p.final_label == p.human_label for p in both if disagrees(p)),
        "draft_correct_on_sample": sum(
            final_label(p) is not None and final_label(p) == p.label for p in both
        ),
        "disagreements": [
            {
                "a": p.a["name"],
                "b": p.b["name"],
                "drafted": p.label,
                "human": p.human_label,
                "rationale": p.rationale,
                "human_note": p.human_note,
                "final": p.final_label,
                "final_note": p.final_note,
            }
            for p in both
            if p.human_label != p.label
        ],
    }


def label_statement(agr: dict[str, Any]) -> str:
    """The one sentence the README and the report must carry about where labels come from."""
    n, done = agr["sample_size"], agr["human_verified"]
    drafted = (
        f"Claude-drafted labels ({agr['drafted']} pairs, drafted blind to the matcher's score "
        "and decision)"
    )
    if agr["agreement"] is None:
        pending = f"{n} drawn for human verification ({done} verified so far)"
        return f"{drafted}, {pending}, agreement pending."
    agreement = f"agreement {agr['agreement']:.0%} ({agr['agree']}/{done})"
    if done < n:
        return f"{drafted}, {done} of {n} human-verified so far, {agreement} (provisional)."
    raw = f"{drafted}, {n} human-verified, {agreement}."
    open_ = done - agr["agree"] - agr["adjudicated"]
    if agr["adjudicated"] == 0:
        return f"{raw} Adjudication of the {done - agr['agree']} disagreements pending."
    after = (
        f" After adjudicating {agr['adjudicated']} of the {done - agr['agree']} disagreements, "
        f"the final label sides with the draft on {agr['final_sides_with_draft']} and with the "
        f"human label on {agr['final_sides_with_human']}"
    )
    neither = agr["adjudicated"] - agr["final_sides_with_draft"] - agr["final_sides_with_human"]
    after += f", neither on {neither}" if neither else ""
    after += f"; {open_} still open." if open_ else "."
    return raw + after


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
    human, _ = judge(conn, region, truth="human")
    human_clustering = metrics([j.truth for j in human], [j.same_cluster for j in human])
    agr = agreement()
    views: dict[str, tuple[Metrics, int]] = {}
    for name, key in (
        ("drafted labels (all pairs)", "drafted"),
        ("human labels (sample, before adjudication)", "human"),
        ("final labels (sample, after adjudication)", "final"),
        ("drafted + adjudicated corrections (all pairs)", "corrected"),
    ):
        js = judged if key == "drafted" else judge(conn, region, truth=key)[0]
        views[name] = (metrics([j.truth for j in js], [j.same_cluster for j in js]), len(js))
    result = {
        "labels": label_statement(agr),
        "n_labelled": len(judged),
        "n_missing_records": missing,
        "positives": sum(truth),
        "clustering": clustering.as_dict(),
        "clustering_vs_human_sample": human_clustering.as_dict() | {"n": len(human)},
        "agreement": {k: v for k, v in agr.items() if k != "disagreements"},
        "threshold_sweep": {str(t): m.as_dict() for t, m in sweep.items()},
        "errors": {k: len(v) for k, v in errors.items()},
        "by_label_set": {k: m.as_dict() | {"n": n} for k, (m, n) in views.items()},
    }
    _write_report(region, result, clustering, sweep, errors, agr, human_clustering, len(human))
    _append_label_sets(views, agr)
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
    agr: dict[str, Any],
    human: Metrics,
    n_human: int,
) -> None:
    lines = [
        "# Entity resolution evaluation",
        "",
        f"Region `{region}`, {date.today().isoformat()}, matching config `{_config_hash()}`.",
        "",
        f"**Labels:** {result['labels']}",
        "",
        f"Gold pairs with a match / non-match label: **{result['n_labelled']}** "
        f"({result['positives']} matches); pairs whose records are missing or no longer "
        f"campgrounds: {result['n_missing_records']}. The drafted labels were written from the "
        "two records alone, each with a one-line rationale; the human sample was drawn at "
        "random and labelled without seeing them. Intervals are Wilson 95%.",
        "",
        "## Clustering (what the app shows)",
        "",
        "| precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |",
        "|---|---|---|---|---|---|---|---|---|",
        _clustering_row(clustering),
        "",
        "## Against the human-verified sample",
        "",
        f"{n_human} pairs with a human match / non-match label.",
        "",
        "| precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |",
        "|---|---|---|---|---|---|---|---|---|",
        _clustering_row(human),
        "",
        "Disagreements between the drafted and the human label:",
        "",
        *(
            f"- {d['a']} vs {d['b']}: drafted {d['drafted']}, human {d['human']} "
            f"(rationale: {d['rationale']}; note: {d['human_note'] or '-'})"
            for d in agr["disagreements"]
        ),
        *(["- none so far"] if not agr["disagreements"] else []),
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
    for title, items in (
        ("False merges", errors["false_merges"]),
        ("Missed matches", errors["missed_matches"]),
    ):
        lines += [f"## {title} ({len(items)})", ""]
        for j in items[:15]:
            contrib = ", ".join(
                f"{k} {v:+g}"
                for k, v in sorted(j.contributions.items(), key=lambda kv: -abs(kv[1]))
            )
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
        {
            k: result[k]
            for k in (
                "labels",
                "n_labelled",
                "positives",
                "clustering",
                "clustering_vs_human_sample",
                "agreement",
                "by_label_set",
                "errors",
            )
        },
        indent=2,
    )


def _append_label_sets(views: dict[str, tuple[Metrics, int]], agr: dict[str, Any]) -> None:
    """Matcher metrics against each label set, and every disagreement with its resolution."""
    lines = [
        "",
        "## Matcher against each label set",
        "",
        "The raw agreement above is kept as measured; adjudication adds a third label per",
        "disagreement and never changes the drafted or the human one.",
        "",
        "| labels | n | precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *(f"| {name} | {n} " + _clustering_row(m) for name, (m, n) in views.items()),
        "",
        "Disagreement kinds (draft -> human): "
        + ", ".join(f"{k}: {v}" for k, v in sorted(agr["disagreement_kinds"].items())),
        "",
        "| A | B | draft | rationale | human | final | why |",
        "|---|---|---|---|---|---|---|",
        *(
            f"| {d['a'] or '(unnamed)'} | {d['b'] or '(unnamed)'} | {d['drafted']} | "
            f"{d['rationale']} | {d['human']} | {d['final'] or 'open'} | {d['final_note'] or ''} |"
            for d in agr["disagreements"]
        ),
        "",
    ]
    with REPORT_PATH.open("a") as f:
        f.write("\n".join(lines))
