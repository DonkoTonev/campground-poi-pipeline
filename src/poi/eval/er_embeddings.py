"""`poi eval er-embeddings`: does the embedding candidate source / feature help ER?

Runs `poi match` three times (embeddings off, as candidate source only, as candidate source
plus scoring feature), scores each clustering against the gold labels, lists every record
pair whose merge decision changes between variants (labelled or not), and writes
reports/er_embeddings.md. Afterwards the match tables are rebuilt with the configured
setting, so the database is left as `poi match` would leave it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Any

from sqlalchemy import Engine, text

from poi.eval.er import Metrics, _ci, _fmt, agreement, judge, label_statement, metrics, wilson
from poi.match import embeddings
from poi.match.scoring import load_config
from poi.match.stage import match_region
from poi.settings import PROJECT_ROOT, Region

REPORT_PATH = PROJECT_ROOT / "reports" / "er_embeddings.md"


@dataclass(frozen=True)
class Variant:
    name: str
    use_embeddings: bool
    feature: bool


VARIANTS = (
    Variant("off (current default)", False, False),
    Variant("candidates only", True, False),
    Variant("candidates + feature", True, True),
)


def _merged_pairs(engine: Engine, region: str) -> set[tuple[int, int]]:
    """Every record pair that ends up in one cluster."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT cluster_key, poi_record_id FROM cluster_members WHERE region = :r"),
            {"r": region},
        ).all()
    clusters: dict[Any, list[int]] = {}
    for key, rid in rows:
        clusters.setdefault(key, []).append(rid)
    return {(a, b) for ids in clusters.values() for a, b in combinations(sorted(ids), 2)}


def _names(engine: Engine, ids: set[int]) -> dict[int, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, source, name FROM poi_records WHERE id = ANY(:ids)"),
            {"ids": list(ids)},
        ).all()
    return {r.id: f"{r.source}:{r.name}" for r in rows}


def run(
    engine: Engine, region: Region, embedder: embeddings.Embedder | None = None
) -> dict[str, Any]:
    cfg = load_config()
    embedder = embedder or embeddings.FastEmbedder(cfg["embeddings"]["model"])
    results: dict[str, dict[str, Any]] = {}
    merged: dict[str, set[tuple[int, int]]] = {}
    gold_truth: dict[tuple[int, int], bool] = {}
    try:
        for v in VARIANTS:
            stats = match_region(
                engine, region, use_embeddings=v.use_embeddings, embedding_feature=v.feature,
                embedder=embedder,
            )  # fmt: skip
            with engine.connect() as conn:
                judged, missing = judge(conn, region.name)
                ids = {
                    (r.source, r.source_id): r.id
                    for r in conn.execute(text("SELECT source, source_id, id FROM poi_records"))
                }
            for j in judged:
                a = ids[(j.pair.a["source"], j.pair.a["source_id"])]
                b = ids[(j.pair.b["source"], j.pair.b["source_id"])]
                gold_truth[(min(a, b), max(a, b))] = j.truth
            truth = [j.truth for j in judged]
            positives = [j for j in judged if j.truth]
            results[v.name] = {
                "clustering": metrics(truth, [j.same_cluster for j in judged]),
                "pair_at_threshold": metrics(
                    truth,
                    [j.score is not None and j.score >= cfg["thresholds"]["match"] for j in judged],
                ),
                "gold_matches_generated": sum(j.score is not None for j in positives),
                "gold_matches": len(positives),
                "n": len(judged),
                "missing": missing,
                "candidate_pairs": stats["candidate_pairs"],
                "clusters": stats["clusters"],
                "embedding_stats": stats.get("embeddings"),
            }
            merged[v.name] = _merged_pairs(engine, region.name)
    finally:
        match_region(engine, region, embedder=embedder if cfg["embeddings"]["enabled"] else None)

    base = VARIANTS[0].name
    changes: dict[str, list[dict[str, Any]]] = {}
    all_ids: set[int] = set()
    for v in VARIANTS[1:]:
        diff = [(p, "merged") for p in sorted(merged[v.name] - merged[base])] + [
            (p, "split") for p in sorted(merged[base] - merged[v.name])
        ]
        changes[v.name] = [{"pair": p, "change": c, "gold": gold_truth.get(p)} for p, c in diff]
        all_ids |= {i for p, _ in diff for i in p}
    names = _names(engine, all_ids) if all_ids else {}
    _write_report(region.name, results, changes, names, embedder.model_name)
    return {
        "labels": label_statement(agreement()),
        "variants": {
            name: {
                "clustering": r["clustering"].as_dict(),
                "gold_matches_generated": f"{r['gold_matches_generated']}/{r['gold_matches']}",
                "clusters": r["clusters"],
                "candidate_pairs": r["candidate_pairs"],
            }
            for name, r in results.items()
        },
        "changed_pairs_vs_off": {
            name: {
                "total": len(c),
                "labelled": sum(x["gold"] is not None for x in c),
            }
            for name, c in changes.items()
        },
    }


def _row(name: str, m: Metrics) -> str:
    p_ci, r_ci = wilson(m.tp, m.tp + m.fp), wilson(m.tp, m.tp + m.fn)
    return (
        f"| {name} | {_fmt(m.precision)} {_ci(p_ci)} | {_fmt(m.recall)} {_ci(r_ci)} "
        f"| {_fmt(m.f1)} | {m.tp} | {m.fp} | {m.fn} |"
    )


def _write_report(
    region: str,
    results: dict[str, dict[str, Any]],
    changes: dict[str, list[dict[str, Any]]],
    names: dict[int, str],
    model: str,
) -> None:
    first = next(iter(results.values()))
    cfg = load_config()["embeddings"]
    lines = [
        "# Embeddings in entity resolution: ablation",
        "",
        f"Region `{region}`, {date.today().isoformat()}, model `{model}` (local, fastembed), "
        f"top {cfg['top_k']} neighbours at cosine ≥ {cfg['min_similarity']} within "
        f"{cfg['max_distance_m'] / 1000:g} km.",
        "",
        f"**Labels:** {label_statement(agreement())}",
        "",
        f"{first['n']} gold pairs with a match / non-match label "
        f"({first['gold_matches']} matches). Intervals are Wilson 95%. The gold pairs were "
        "sampled from the blocking candidates that existed before embeddings (plus a stratum "
        "of similar names blocking missed), so pairs that only embeddings find are "
        "under-represented; every changed decision is listed below, labelled or not.",
        "",
        "## Clustering against the labels",
        "",
        "| variant | precision [95% CI] | recall [95% CI] | F1 | TP | FP | FN |",
        "|---|---|---|---|---|---|---|",
        *(_row(name, r["clustering"]) for name, r in results.items()),
        "",
        "## Candidates and pair scores",
        "",
        "| variant | candidate pairs | gold matches generated | pair P / R at the match "
        "threshold | clusters |",
        "|---|---|---|---|---|",
        *(
            f"| {name} | {r['candidate_pairs']} | {r['gold_matches_generated']}/"
            f"{r['gold_matches']} | {_fmt(r['pair_at_threshold'].precision)} / "
            f"{_fmt(r['pair_at_threshold'].recall)} | {r['clusters']} |"
            for name, r in results.items()
        ),
        "",
    ]
    for name, items in changes.items():
        lines += [f"## Merge decisions that change: {name} vs off ({len(items)})", ""]
        for x in items:
            a, b = x["pair"]
            gold = {None: "unlabelled", True: "label: match", False: "label: non-match"}[x["gold"]]
            lines.append(f"- {x['change']}: {names.get(a, a)} + {names.get(b, b)} ({gold})")
        if not items:
            lines.append("- none")
        lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines))
