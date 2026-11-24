"""`poi` command-line entry point. One sub-command per pipeline stage."""

from __future__ import annotations

import typer

from poi.db import check_extensions, get_engine
from poi.log import configure_logging, get_logger
from poi.settings import get_settings, load_region

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
