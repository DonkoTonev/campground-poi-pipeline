"""`poi review label`: verify gold-set pairs side by side in the terminal.

The model's suggestion is hidden unless asked for (`--show-suggestion`), so it does not
anchor the reviewer. Progress is saved after every answer.
"""

from __future__ import annotations

import math
from typing import Any

from rich.console import Console
from rich.table import Table
from sqlalchemy import Connection, text

from poi.review.gold import GoldPair, load, mark_verified, save

FIELDS = (
    "name", "alt_names", "category", "is_campground", "operator_name", "phone", "website",
    "reservation_url", "ids", "lat", "lon", "precision_m", "num_sites", "months_open",
)  # fmt: skip
CHOICES = {"m": "match", "n": "non_match", "u": "unsure"}


def _record(conn: Connection, ref: dict[str, str]) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT name, alt_names, category, is_campground, operator_name, phone, website, "
                "reservation_url, ids, ST_Y(geom) AS lat, ST_X(geom) AS lon, precision_m, "
                "num_sites, months_open FROM poi_records WHERE source = :s AND source_id = :i"
            ),
            {"s": ref["source"], "i": ref["source_id"]},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _distance_m(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    if None in (a["lat"], a["lon"], b["lat"], b["lon"]):
        return None
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dl = math.radians(b["lon"] - a["lon"])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def map_link(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """OpenStreetMap page showing both points (as a route's start and end markers)."""
    if None in (a["lat"], a["lon"], b["lat"], b["lon"]):
        point = a if a["lat"] is not None else b
        if point["lat"] is None:
            return None
        return f"https://www.openstreetmap.org/?mlat={point['lat']:.5f}&mlon={point['lon']:.5f}#map=16/{point['lat']:.5f}/{point['lon']:.5f}"
    return (
        "https://www.openstreetmap.org/directions?route="
        f"{a['lat']:.5f}%2C{a['lon']:.5f}%3B{b['lat']:.5f}%2C{b['lon']:.5f}"
    )


def _fmt(value: Any) -> str:
    if value is None or value == [] or value == {}:
        return "-"
    if isinstance(value, float):
        return f"{value:.5f}"
    return str(value)


def render(console: Console, pair: GoldPair, a: dict[str, Any], b: dict[str, Any],
           index: int, total: int, show_suggestion: bool) -> None:  # fmt: skip
    table = Table(title=f"Pair {index}/{total}  ·  strata: {', '.join(pair.strata)}")
    table.add_column("field", style="bold")
    table.add_column(f"A · {pair.a['source']}:{pair.a['source_id']}")
    table.add_column(f"B · {pair.b['source']}:{pair.b['source_id']}")
    for f in FIELDS:
        table.add_row(f, _fmt(a.get(f)), _fmt(b.get(f)))
    console.print(table)
    d = _distance_m(a, b)
    console.print(f"distance: {'unknown' if d is None else f'{d:,.0f} m'}")
    if link := map_link(a, b):
        console.print(f"map: {link}")
    if show_suggestion:
        console.print(
            f"[dim]model suggestion: {pair.suggested} (score {pair.suggestion_score})[/dim]"
        )


def run(conn: Connection, show_suggestion: bool = False, relabel: bool = False) -> dict[str, int]:
    console = Console()
    pairs = load()
    todo = [p for p in pairs if relabel or not p.verified]
    console.print(f"{len(todo)} pairs to review ({len(pairs)} in gold set). "
                  r"\[m]atch  \[n]on-match  \[u]nsure  \[s]kip  \[q]uit")  # fmt: skip
    done = 0
    for i, pair in enumerate(todo, start=1):
        a, b = _record(conn, pair.a), _record(conn, pair.b)
        if a is None or b is None:
            console.print(f"[yellow]skipping {pair.key}: record not in database[/yellow]")
            continue
        render(console, pair, a, b, i, len(todo), show_suggestion)
        while (answer := console.input("label> ").strip().lower()) not in ("m", "n", "u", "s", "q"):
            console.print("type m, n, u, s or q")
        if answer == "q":
            break
        if answer == "s":
            continue
        note = console.input("note (optional)> ").strip() or None
        mark_verified(pair, CHOICES[answer], note)
        save(pairs)  # after every answer: quitting never loses work
        done += 1
    verified = sum(p.verified for p in pairs)
    console.print(f"labelled {done} this session; {verified}/{len(pairs)} verified")
    return {"labelled": done, "verified": verified, "total": len(pairs)}
