"""`poi` command-line entry point. One sub-command per pipeline stage."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from poi.db import check_extensions, get_engine
from poi.ingest import ingest_source
from poi.log import configure_logging, get_logger
from poi.normalize.stage import normalize_region
from poi.settings import get_settings, load_region
from poi.sources import ALL_SOURCES
from poi.storage import make_store

app = typer.Typer(no_args_is_help=True, help="Campground POI pipeline.")
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
