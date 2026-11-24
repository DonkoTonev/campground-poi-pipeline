"""`poi` command-line entry point. One sub-command per pipeline stage."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from poi.crawl.stage import crawl_region
from poi.db import check_extensions, get_engine
from poi.eval import er as er_eval
from poi.ingest import ingest_source
from poi.log import configure_logging, get_logger
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.report import quality as reports
from poi.resolve.stage import resolve_region
from poi.review import gold, label, queue
from poi.settings import PROJECT_ROOT, get_settings, load_region
from poi.sources import ALL_SOURCES
from poi.storage import make_store

app = typer.Typer(no_args_is_help=True, help="Campground POI pipeline.")
review_app = typer.Typer(no_args_is_help=True, help="Human-in-the-loop review and labelling.")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluation against verified gold sets.")
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
) -> None:
    """Entity resolution: block, score, cluster and relate campground records."""
    settings = get_settings()
    stats = match_region(get_engine(), load_region(region, settings))
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


@review_app.command("label")
def review_label(
    show_suggestion: Annotated[
        bool, typer.Option(help="Show the model's suggestion (off: avoids anchoring).")
    ] = False,
    relabel: Annotated[bool, typer.Option(help="Also revisit verified pairs.")] = False,
) -> None:
    """Verify gold pairs side by side: [m]atch, [n]on-match, [u]nsure, [s]kip, [q]uit."""
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
        typer.echo(reports.write("quality.md", reports.quality_report(conn, reg.name)))


@report_app.command("changes")
def report_changes(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """New, merged, retired and changed entities since the previous resolve run."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        typer.echo(reports.write("changes.md", reports.changes_report(conn, reg.name)))


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


if __name__ == "__main__":
    app()
