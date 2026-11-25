"""`poi report cost`: what extraction cost, per input, and what it would cost at scale.

All token numbers come from the `llm_calls` ledger (the provider's own counts) and the latest
reduce run. Prices come from config/pricing.yaml. Until a live run has happened, the report
says so and shows no extraction numbers.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Connection, text

from poi.extract.llm.base import Usage
from poi.extract.pricing import HAIKU, billed


def _haiku(tokens_in: float, tokens_out: float, batch: bool = False) -> float:
    """Haiku list price of ONE request (pricing rules such as long context are per prompt)."""
    return billed("anthropic", HAIKU, Usage(int(tokens_in), int(tokens_out)), batch=batch)


def cost_report(conn: Connection, region: str) -> str:
    calls = conn.execute(
        text(
            """
            SELECT provider, model, ladder_step, count(*) AS n,
                   sum(input_tokens + cache_read_tokens) AS inp, sum(cache_read_tokens) AS cached,
                   sum(output_tokens) AS out, sum(thinking_tokens) AS think,
                   sum(cost_usd) AS billed, sum(est_haiku_usd) AS haiku,
                   round(avg(latency_ms)) AS latency
            FROM llm_calls WHERE purpose = 'extract' AND NOT replayed
            GROUP BY 1, 2, 3 ORDER BY 1, 3 DESC
            """
        )
    ).all()
    kinds = conn.execute(
        text(
            """
            SELECT d.kind, count(DISTINCT d.id) AS docs,
                   sum(c.input_tokens + c.cache_read_tokens) AS inp, sum(c.output_tokens) AS out,
                   sum(c.est_haiku_usd) AS haiku,
                   count(*) FILTER (WHERE c.ladder_step = 'strong') AS strong
            FROM llm_calls c JOIN documents d ON d.id = c.document_id
            WHERE c.purpose = 'extract' AND NOT c.replayed GROUP BY 1 ORDER BY 1
            """
        )
    ).all()
    outcomes = conn.execute(
        text(
            "SELECT d.kind, x.status, x.ladder_step, count(*) AS n, "
            "avg(x.hallucination_rate) AS halluc FROM extractions x "
            "JOIN documents d ON d.id = x.document_id GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        )
    ).all()
    methods = conn.execute(
        text(
            """
            SELECT m.value AS method, count(*) AS n
            FROM extractions x, jsonb_array_elements(coalesce(x.output, '[]'::jsonb)) c,
                 jsonb_each_text(c->'_methods') m GROUP BY 1 ORDER BY 2 DESC
            """
        )
    ).all()
    campgrounds = conn.execute(
        text("SELECT count(*) FROM source_records WHERE source IN ('web', 'ridb_description')")
    ).scalar_one()
    reduce_stats = conn.execute(
        text(
            "SELECT stats FROM runs WHERE stage = 'reduce' AND status = 'succeeded' "
            "ORDER BY id DESC LIMIT 1"
        )
    ).scalar()
    total_docs = sum(k.docs for k in kinds) or 1
    total_in = sum(float(c.inp or 0) for c in calls)
    total_out = sum(float(c.out or 0) + float(c.think or 0) for c in calls)
    haiku_total = sum(float(c.haiku or 0) for c in calls)
    strong = sum(c.n for c in calls if c.ladder_step == "strong")
    cached = sum(float(c.cached or 0) for c in calls)

    providers = sorted({c.provider for c in calls})
    if not calls:
        status = (
            "**The LLM extraction stage is implemented and tested with fixtures, but has "
            "not been run live yet.** No extraction cost has been measured; this report "
            "fills in after the first live run."
        )
    else:
        status = (
            f"Live extraction calls in the ledger: provider(s) {', '.join(providers)}. "
            "Billed amounts are list prices from `config/pricing.yaml`; the Haiku column "
            "prices the same token counts on Claude Haiku 5.5 without caching (a reference "
            "point for Claude calls, an estimate for other providers)."
        )
    lines = [
        "# Cost report",
        "",
        f"Region `{region}`, generated {date.today().isoformat()} from the `llm_calls` ledger.",
        "",
        status,
        "",
        "## Calls",
        "",
        "| provider | model | step | calls | input tokens | output tokens | thinking | billed "
        "| est. on Haiku | avg latency |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *(
            f"| {c.provider} | {c.model} | {c.ladder_step} | {c.n} | {int(c.inp or 0):,} | "
            f"{int(c.out or 0):,} | {int(c.think or 0):,} | ${float(c.billed or 0):.4f} | "
            f"${float(c.haiku or 0):.4f} | {int(c.latency or 0):,} ms |"
            for c in calls
        ),
        "",
        f"Escalation rate: {strong} of {total_docs} documents ({strong / total_docs:.0%}) went "
        f"to the strong model. Cache hits: {cached / total_in:.0%} of input tokens were read "
        "from the provider's cache."
        if total_in
        else "No live extraction calls yet.",
        "",
        "## By input (provenance)",
        "",
        "| input | documents | input tokens / doc | output tokens / doc | est. Haiku $ / doc "
        "| escalations |",
        "|---|---|---|---|---|---|",
        *(
            f"| {k.kind} | {k.docs} | {int((k.inp or 0) / k.docs):,} | "
            f"{int((k.out or 0) / k.docs):,} | ${float(k.haiku or 0) / k.docs:.6f} | {k.strong} |"
            for k in kinds
        ),
        "",
        "## Outcomes",
        "",
        "| input | status | answer from | documents | mean hallucination rate |",
        "|---|---|---|---|---|",
        *(
            f"| {o.kind} | {o.status} | {o.ladder_step} | {o.n} | "
            f"{'n/a' if o.halluc is None else f'{o.halluc:.1%}'} |"
            for o in outcomes
        ),
        "",
        "Which ladder step produced each extracted value:",
        "",
        "| method | values |",
        "|---|---|",
        *(f"| {m.method} | {m.n} |" for m in methods),
        "",
    ]
    if campgrounds:
        per_poi = haiku_total / campgrounds
        lines += [
            f"Est. Claude Haiku cost per 1,000 extracted campgrounds: ${per_poi * 1000:.4f} "
            f"({campgrounds} campgrounds extracted).",
            "",
        ]
    if reduce_stats and total_docs and total_in:
        pages = reduce_stats["pages"] or 1
        per_page_out = total_out / total_docs
        prompt_overhead = max(
            0.0,
            total_in / total_docs - reduce_stats["bundle_tokens"] / max(reduce_stats["bundles"], 1),
        )
        lines += [
            "## Extrapolation to 1M pages (estimate)",
            "",
            "Per-page tokens by representation from the reduce stage (bytes/4 estimates), plus "
            f"the measured average prompt overhead per request ({int(prompt_overhead):,} tokens: "
            f"instructions, schema, wrapper) and output ({int(per_page_out):,} tokens per "
            "request), priced on Claude Haiku 5.5. One page per request; bundling pages per site "
            "pays the overhead once per site instead.",
            "",
            "| representation | tokens / page | 1M pages, Haiku | 1M pages, Haiku batch |",
            "|---|---|---|---|",
        ]
        for key, label in (
            ("raw_html", "raw HTML"),
            ("cleaned_html", "cleaned HTML"),
            ("markdown", "Markdown"),
            ("text", "plain text"),
        ):
            per_page = reduce_stats[f"page_tokens:{key}"] / pages
            n = 1_000_000
            # Priced per request, then scaled: long-context pricing applies per prompt.
            cost = _haiku(per_page + prompt_overhead, per_page_out) * n
            batch = _haiku(per_page + prompt_overhead, per_page_out, batch=True) * n
            lines.append(f"| {label} | {int(per_page):,} | ${cost:,.0f} | ${batch:,.0f} |")
        lines += [
            "",
            "Raw HTML pages average more than 36k tokens; at that size many real pages cross "
            "Haiku's 100k-token long-context threshold when bundled, which this table does not "
            "even count.",
            "",
        ]
    return "\n".join(lines)
