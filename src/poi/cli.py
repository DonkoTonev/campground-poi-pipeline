"""`poi` command-line entry point. One sub-command per pipeline stage."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from poi.db import check_extensions, get_engine
from poi.eval import er as er_eval
from poi.ingest import ingest_source
from poi.log import configure_logging, get_logger
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.review import gold, label
from poi.settings import PROJECT_ROOT, get_settings, load_region
from poi.sources import ALL_SOURCES
from poi.storage import make_store

app = typer.Typer(no_args_is_help=True, help="Campground POI pipeline.")
review_app = typer.Typer(no_args_is_help=True, help="Human-in-the-loop review and labelling.")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluation against verified gold sets.")
app.add_typer(review_app, name="review")
app.add_typer(eval_app, name="eval")
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
    typer.echo(f"{len(pairs)} pairs in {gold.GOLD_PATH.relative_to(PROJECT_ROOT)} "
               f"({sum(p.verified for p in pairs)} verified)")  # fmt: skip


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


@eval_app.command("er")
def eval_er(region: Annotated[str | None, typer.Option(help="Region name.")] = None) -> None:
    """Precision/recall/F1 of ER on verified labels; writes reports/er_eval.md."""
    reg = load_region(region, get_settings())
    with get_engine().connect() as conn:
        result = er_eval.evaluate(conn, reg.name)
    typer.echo(er_eval.summary_json(result))
