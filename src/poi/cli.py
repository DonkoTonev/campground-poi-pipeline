"""`poi` command-line entry point. One sub-command per pipeline stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, cast

import typer

from poi.crawl.stage import crawl_region
from poi.db import check_extensions, get_engine
from poi.eval import er as er_eval
from poi.eval import extraction as extraction_eval
from poi.extract.llm.replay import Mode
from poi.extract.stage import build_client, dry_run, extract_region
from poi.ingest import ingest_source
from poi.log import configure_logging, get_logger
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.reduce.stage import reduce_region
from poi.report import cost as cost_report
from poi.report import quality as quality_report
from poi.report import tokens as token_report
from poi.report import write as write_report
from poi.resolve.stage import resolve_region
from poi.review import drafts, gold, label, queue
from poi.settings import PROJECT_ROOT, get_settings, load_region
from poi.sources import ALL_SOURCES
from poi.storage import make_store

app = typer.Typer(no_args_is_help=True, help="Campground POI pipeline.")
review_app = typer.Typer(no_args_is_help=True, help="Human-in-the-loop review and labelling.")
eval_app = typer.Typer(
    no_args_is_help=True,
    help="Evaluation against the gold sets (ER labels, synthetic extraction cases).",
)
app.add_typer(review_app, name="review")
app.add_typer(eval_app, name="eval")
report_app = typer.Typer(no_args_is_help=True, help="Markdown reports in reports/.")
app.add_typer(report_app, name="report")
log = get_logger(__name__)


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)


@app.command()
def doctor() -> None:
    """Check config, database extensions and which API keys are configured (never prints them)."""
    settings = get_settings()
    region = load_region(settings=settings)
    typer.echo(f"region: {region.name} bbox={region.bbox}")
    typer.echo(f"contact_url set: {bool(settings.contact_url)}")
    for key in ("anthropic_api_key", "ridb_api_key", "nps_api_key"):
        typer.echo(f"{key}: {'set' if getattr(settings, key) else 'missing'}")
    try:
        exts = check_extensions(get_engine())
        typer.echo(f"postgres extensions: {exts or 'none (run make migrate)'}")
    except Exception as exc:  # report, don't crash: doctor is a diagnostic
        typer.echo(f"postgres: unreachable ({type(exc).__name__})")


@app.command()
def ingest(
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Repeatable; default: all.")
    ] = None,
    region: Annotated[
        str | None, typer.Option(help="Region name from config/regions.yaml.")
    ] = None,
    refresh: Annotated[
        bool, typer.Option(help="Re-fetch even if the raw store already has the response.")
    ] = False,
) -> None:
    """Fetch structured sources (raw store first) and upsert source_records."""
    settings = get_settings()
    reg = load_region(region, settings)
    names = source or list(ALL_SOURCES)
    unknown = set(names) - set(ALL_SOURCES)
    if unknown:
        raise typer.BadParameter(f"unknown source(s): {sorted(unknown)}")
    store = make_store(settings)
    for name in names:
        stats = ingest_source(get_engine(), store, settings, reg, name, refresh)
        typer.echo(f"{name}: {stats}")


@app.command()
def normalize(
    region: Annotated[
        str | None, typer.Option(help="Region name from config/regions.yaml.")
    ] = None,
) -> None:
    """Map source_records into canonical poi_records and write quality_flags."""
    settings = get_settings()
    stats = normalize_region(get_engine(), load_region(region, settings))
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def match(
    region: Annotated[
        str | None, typer.Option(help="Region name from config/regions.yaml.")
    ] = None,
    embeddings: Annotated[
        bool | None,
        typer.Option(
            "--embeddings/--no-embeddings",
            help="Name embeddings as candidate source and feature (default: matching.yaml).",
        ),
    ] = None,
) -> None:
    """Entity resolution: block, score, cluster and relate campground records."""
    settings = get_settings()
    stats = match_region(get_engine(), load_region(region, settings), use_embeddings=embeddings)
    typer.echo(json.dumps(stats, indent=2))


@review_app.command("suggest-gold")
def suggest_gold(
    region: Annotated[str | None, typer.Option(help="Region name.")] = None,
    target: Annotated[int, typer.Option(help="About how many pairs to sample.")] = 200,
) -> None:
    """Sample ER gold pairs with suggested (unverified) labels; keeps existing labels."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        new = gold.suggest(conn, reg.name, target)
    pairs = gold.merge(gold.load(), new)
    gold.save(pairs)
    typer.echo(
        f"{len(pairs)} pairs in {gold.GOLD_PATH.relative_to(PROJECT_ROOT)} "
        f"({sum(p.verified for p in pairs)} verified)"
    )


@review_app.command("export-blind")
def review_export_blind(
    out_dir: Annotated[Path, typer.Argument(help="Directory for the blind pair files.")],
    parts: Annotated[int, typer.Option(help="Number of files to split the pairs into.")] = 2,
) -> None:
    """Gold pairs as the two records plus distance only (no score, decision or strata)."""
    with get_engine().connect() as conn:
        paths = drafts.export_blind(conn, gold.load(), out_dir, parts)
    typer.echo("\n".join(str(p) for p in paths))


@review_app.command("import-drafts")
def review_import_drafts(
    files: Annotated[list[Path], typer.Argument(help='JSONL: {"pair", "label", "rationale"}.')],
    by: Annotated[str, typer.Option(help="Who drafted the labels (stored as labeled_by).")],
    force: Annotated[bool, typer.Option(help="Overwrite existing drafted labels.")] = False,
) -> None:
    """Store blind-drafted labels with their rationales in the gold file."""
    pairs = gold.load()
    try:
        n = drafts.import_drafts(pairs, files, by, force)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    gold.save(pairs)
    typer.echo(f"{n} drafted labels stored")


@review_app.command("draw-sample")
def review_draw_sample(
    n: Annotated[int, typer.Option(help="Pairs for human verification.")] = 50,
    seed: Annotated[int, typer.Option(help="Random seed (recorded in the docs).")] = 20261124,
) -> None:
    """Pick the random pairs a person verifies with `poi review label`; never redraws."""
    pairs = gold.load()
    try:
        sample = drafts.draw_sample(pairs, n, seed)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    gold.save(pairs)
    typer.echo(f"{len(sample)} pairs drawn for human verification")


@review_app.command("label")
def review_label(
    show_suggestion: Annotated[
        bool, typer.Option(help="Show the model's suggestion (off: avoids anchoring).")
    ] = False,
    relabel: Annotated[bool, typer.Option(help="Also revisit pairs you already labelled.")] = False,
) -> None:
    """Label the human-verification sample blind: m match, n non-match, u unsure, s skip, q quit."""
    with get_engine().connect() as conn:
        label.run(conn, show_suggestion, relabel)


@review_app.command("queue")
def review_queue(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Decide gray-zone pairs and field conflicts; decisions go to data/overrides.yaml."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        queue.run(conn, reg.name)


@eval_app.command("er")
def eval_er(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Precision/recall/F1 of ER on verified labels; writes reports/er_eval.md."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        result = er_eval.evaluate(conn, reg.name)
    typer.echo(er_eval.summary_json(result))


@eval_app.command("er-embeddings")
def eval_er_embeddings(
    region: Annotated[str | None, typer.Option(help="Region name.")] = None,
) -> None:
    """Ablation: ER with and without name embeddings; writes reports/er_embeddings.md."""
    from poi.eval import er_embeddings

    result = er_embeddings.run(get_engine(), load_region(region, get_settings()))
    typer.echo(json.dumps(result, indent=2))


@app.command()
def resolve(
    region: Annotated[
        str | None, typer.Option(help="Region name from config/regions.yaml.")
    ] = None,
) -> None:
    """Golden records: stable entity ids, field survivorship with provenance, quality score."""
    settings = get_settings()
    stats = resolve_region(get_engine(), load_region(region, settings))
    typer.echo(json.dumps(stats, indent=2))


@report_app.command("quality")
def report_quality(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Coverage, flags, conflicts, freshness, quality score and top data gaps."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        typer.echo(write_report("quality.md", quality_report.quality_report(conn, reg.name)))


@report_app.command("changes")
def report_changes(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """New, merged, retired and changed entities since the previous resolve run."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        typer.echo(write_report("changes.md", quality_report.changes_report(conn, reg.name)))


@app.command()
def crawl(
    region: Annotated[str | None, typer.Option(help="Region name.")] = None,
    plan: Annotated[
        bool, typer.Option(help="Screen and record seeds only; fetch nothing.")
    ] = False,
    render: Annotated[
        bool, typer.Option(help="Render thin JS-only pages with Playwright.")
    ] = False,
    max_sites: Annotated[int | None, typer.Option(help="Crawl at most this many sites.")] = None,
    seed: Annotated[list[str] | None, typer.Option(help="Extra seed URL (repeatable).")] = None,
    http_cache: Annotated[bool, typer.Option(help="Scrapy HTTP cache (development).")] = False,
) -> None:
    """Crawl official campground websites responsibly; every outcome is recorded."""
    settings = get_settings()
    stats = crawl_region(
        get_engine(),
        settings,
        load_region(region, settings),
        plan_only=plan,
        render=render,
        max_sites=max_sites,
        extra_seeds=seed,
        http_cache=http_cache,
        allow_private=settings.crawl_allow_private_hosts,
    )
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def reduce(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Crawled pages and RIDB descriptions → capped LLM input documents, with token metrics."""
    settings = get_settings()
    stats = reduce_region(get_engine(), settings, load_region(region, settings))
    typer.echo(json.dumps(stats, indent=2))


@report_app.command("tokens")
def report_tokens(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Tokens per representation (raw HTML vs cleaned vs Markdown vs text) and documents."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        typer.echo(write_report("tokens.md", token_report.tokens_report(conn, reg.name)))


@app.command()
def extract(
    region: Annotated[str | None, typer.Option(help="Region name.")] = None,
    dry_run_: Annotated[
        bool, typer.Option("--dry-run", help="Count tokens and estimate cost; generate nothing.")
    ] = False,
    mode: Annotated[
        str, typer.Option(help="replay: recordings only; record: call when missing; live.")
    ] = "record",
    provider: Annotated[str | None, typer.Option(help="gemini or anthropic.")] = None,
    kind: Annotated[str | None, typer.Option(help="web_bundle or ridb_description.")] = None,
    limit: Annotated[int | None, typer.Option(help="At most this many documents.")] = None,
    max_cost_usd: Annotated[
        float | None,
        typer.Option(
            help="Hard stop: never spend more than this in one run (required unless replaying)."
        ),
    ] = None,
    batch: Annotated[
        bool, typer.Option(help="Message Batches API for the cheap step (Anthropic, 50% off).")
    ] = False,
) -> None:
    """LLM extraction over reduced documents (ladder, grounding, ledger)."""
    if mode not in ("replay", "record", "live"):
        raise typer.BadParameter("mode must be replay, record or live")
    settings = get_settings()
    reg = load_region(region, settings)
    engine = get_engine()
    if dry_run_:
        setup = build_client(engine, settings, "record", provider)
        typer.echo(json.dumps(dry_run(engine, settings, setup, kind, limit), indent=2))
        return
    if mode != "replay" and max_cost_usd is None:
        raise typer.BadParameter(
            "a run that can call the API needs --max-cost-usd (run --dry-run first for an estimate)"
        )
    stats = extract_region(
        engine,
        settings,
        reg,
        mode=cast(Mode, mode),
        provider=provider,
        kind=kind,
        limit=limit,
        max_cost_usd=max_cost_usd,
        batch=batch,
    )
    typer.echo(json.dumps(stats, indent=2))


@eval_app.command("extraction")
def eval_extraction(
    live: Annotated[bool, typer.Option(help="Call the provider for missing recordings.")] = False,
    provider: Annotated[str | None, typer.Option(help="gemini or anthropic.")] = None,
) -> None:
    """Per-field accuracy, hallucination bait, cost per page; writes reports/extraction_eval.md."""
    setup = build_client(get_engine(), get_settings(), "record" if live else "replay", provider)
    result = extraction_eval.evaluate(get_engine(), setup)
    typer.echo(json.dumps({k: v for k, v in result.items() if k != "errors"}, indent=2))


@eval_app.command("ablation")
def eval_ablation(
    live: Annotated[bool, typer.Option(help="Call the provider for missing recordings.")] = False,
    provider: Annotated[str | None, typer.Option(help="gemini or anthropic.")] = None,
) -> None:
    """Raw HTML vs cleaned HTML vs Markdown vs text: tokens and accuracy (reports/ablation.md)."""
    setup = build_client(get_engine(), get_settings(), "record" if live else "replay", provider)
    rows = extraction_eval.ablation(get_engine(), setup)
    typer.echo(
        json.dumps(
            {
                r: {k: v for k, v in m.items() if k not in ("errors", "per_field")}
                for r, m in rows.items()
            },
            indent=2,
        )
    )


@report_app.command("cost")
def report_cost(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Ledger totals, per-input cost, escalation and cache rates, Claude estimates, 1M pages."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        typer.echo(write_report("cost.md", cost_report.cost_report(conn, reg.name)))


@app.command()
def index(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Build a new versioned search index of active entities and swap the alias to it."""
    from elasticsearch import Elasticsearch

    from poi.serve.index import build

    settings = get_settings()
    stats = build(
        get_engine(), Elasticsearch(settings.elasticsearch_url), load_region(region, settings)
    )
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:
    """Run the API (OpenAPI docs at /docs)."""
    import uvicorn

    uvicorn.run("poi.serve.api:app", host=host, port=port)


@app.command()
def mcp(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """MCP server over stdio: search_campgrounds, get_campground, along_route (read-only)."""
    from elasticsearch import Elasticsearch

    from poi.serve.index import alias_name
    from poi.serve.mcp_server import build_server

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json, stderr=True)  # stdout is the protocol
    name = load_region(region, settings).name
    server = build_server(
        get_engine(), Elasticsearch(settings.elasticsearch_url), name, alias_name(name)
    )
    server.run("stdio")


@app.command()
def demo() -> None:
    """Pipeline on the committed snapshot (no keys): load → normalize → match → resolve → index."""
    from poi.demo import run_demo

    result = run_demo(get_settings())
    base = "http://127.0.0.1:8000"
    typer.echo(json.dumps(result, indent=2))
    typer.echo(f"""
Try (after `poi serve`, or `make demo` which starts it):
  {base}/docs
  {base}/campgrounds/search?q=tuolume%20medows
  {base}/campgrounds/search?lat=37.74&lon=-119.57&radius_km=30&open_in_month=1
  {base}/campgrounds/bbox?min_lat=37.70&min_lon=-119.62&max_lat=37.76&max_lon=-119.53
  {base}/campgrounds/{result["example_id"]}
  curl -X POST {base}/campgrounds/along-route -H 'content-type: application/json' \\
    -d '{{"route": [[-119.86, 37.70], [-119.59, 37.74], [-119.35, 37.87]], "buffer_km": 3}}'

Same data as MCP tools for Claude: see "Use it from Claude (MCP)" in the README.
""")


if __name__ == "__main__":
    app()
