"""`poi report tokens`: how much smaller each representation is, from the latest reduce run."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Connection, text

REPS = (
    ("raw_html", "raw HTML"),
    ("cleaned_html", "cleaned HTML"),
    ("markdown", "Markdown (+ salvaged facts)"),
    ("text", "plain text"),
)


def tokens_report(conn: Connection, region: str) -> str:
    run = conn.execute(
        text(
            "SELECT id, stats, started_at FROM runs WHERE stage = 'reduce' AND region = :r "
            "AND status = 'succeeded' ORDER BY id DESC LIMIT 1"
        ),
        {"r": region},
    ).first()
    if run is None:
        return "# Token report\n\nNo reduce run yet.\n"
    s = run.stats
    pages = s.get("pages", 0) or 1
    raw = s["page_tokens:raw_html"]
    docs = conn.execute(
        text(
            "SELECT kind, count(*) AS n, round(avg(est_tokens)) AS avg, max(est_tokens) AS max, "
            "sum(est_tokens) AS total FROM documents GROUP BY kind ORDER BY kind"
        )
    ).all()
    lines = [
        "# Token report",
        "",
        f"Region `{region}`, reduce run {run.id} ({run.started_at:%Y-%m-%d}), "
        f"generated {date.today().isoformat()}.",
        "",
        f"Token counts are **estimates** (`{s['token_method']}`); the extraction stage",
        "calibrates them against the API's `count_tokens` endpoint.",
        "",
        f"## Per page ({s['pages']} crawled HTML pages)",
        "",
        "| representation | total tokens | per page | vs raw HTML |",
        "|---|---|---|---|",
        *(
            f"| {label} | {s[f'page_tokens:{key}']:,} | {s[f'page_tokens:{key}'] // pages:,} "
            f"| {s[f'page_tokens:{key}'] / raw:.1%} |"
            for key, label in REPS
        ),
        "",
        "Facts surviving reduction (distinct values in the whole cleaned page vs. in the final",
        "Markdown; a free proxy, the LLM evaluation measures real accuracy):",
        "",
        "| fact | in page | kept | share |",
        "|---|---|---|---|",
        *(
            f"| {k} | {s.get(f'facts_in_page:{k}', 0)} | {s.get(f'facts_kept:{k}', 0)} | "
            f"{s.get(f'facts_kept:{k}', 0) / max(s.get(f'facts_in_page:{k}', 0), 1):.0%} |"
            for k in ("price", "phone", "month")
        ),
        "",
        "## LLM input documents",
        "",
        "| kind | documents | avg tokens | max tokens | total tokens |",
        "|---|---|---|---|---|",
        *(f"| {d.kind} | {d.n} | {d.avg:,} | {d.max:,} | {d.total:,} |" for d in docs),
        "",
        f"Site bundles: {s['bundle_tokens_before']:,} tokens of page Markdown became "
        f"{s['bundle_tokens']:,} after removing {s['boilerplate_lines_removed']} repeated "
        f"lines (menus, footers); {s['bundles_truncated']} of {s['bundles']} bundles hit the "
        "12,000-token cap and were cut by relevance.",
        "",
        "Deterministic facts found without any LLM: "
        + ", ".join(f"{n} via {m}" for m, n in sorted(s["deterministic_facts"].items()))
        + ".",
        "",
    ]
    return "\n".join(lines)
