"""`poi review adjudicate`: decide the pairs where the drafted and the human label differ.

Unlike `poi review label`, nothing is hidden here: the person sees both records, the drafted
label with its rationale and their own earlier label, and records a final label. The final
label is stored separately; the drafted and the human label are never changed, so the raw
agreement stays reproducible.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from sqlalchemy import Connection

from poi.review.gold import GoldPair, disagrees, load, mark_final, save
from poi.review.label import CHOICES, DEFINITION, record_row, render_pair

ID_KINDS = ("recgov_facility_id", "nps_id", "wikidata")


def shared_ids(a: dict[str, Any], b: dict[str, Any]) -> str:
    ia, ib = a.get("ids") or {}, b.get("ids") or {}
    same = [f"{k}={ia[k]}" for k in ID_KINDS if ia.get(k) and ia.get(k) == ib.get(k)]
    differ = [
        f"{k} {ia[k]} vs {ib[k]}" for k in ID_KINDS if ia.get(k) and ib.get(k) and ia[k] != ib[k]
    ]
    if same:
        return "shared: " + ", ".join(same)
    return "different: " + ", ".join(differ) if differ else "none shared"


def todo(pairs: list[GoldPair], redo: bool) -> list[GoldPair]:
    return [p for p in pairs if disagrees(p) and (redo or p.final_label is None)]


def run(conn: Connection, by: str, redo: bool = False) -> dict[str, int]:
    console = Console()
    pairs = load()
    queue = todo(pairs, redo)
    console.print(
        f"{len(queue)} disagreements to adjudicate. "
        r"\[m]atch  \[n]on-match  \[u]nsure  \[s]kip  \[q]uit"
    )
    console.print(f"[bold]{DEFINITION}[/bold]")
    done = 0
    for i, pair in enumerate(queue, start=1):
        a, b = record_row(conn, pair.a), record_row(conn, pair.b)
        conn.rollback()
        if a is None or b is None:
            console.print(f"[yellow]skipping {pair.key}: record not in database[/yellow]")
            continue
        render_pair(
            console,
            f"Disagreement {i}/{len(queue)}",
            f"{pair.a['source']}:{pair.a['source_id']}",
            f"{pair.b['source']}:{pair.b['source_id']}",
            a,
            b,
        )
        console.print(f"ids: {shared_ids(a, b)}")
        console.print(f"draft ({pair.labeled_by}): [bold]{pair.label}[/bold] - {pair.rationale}")
        note = f" - {pair.human_note}" if pair.human_note else ""
        console.print(f"your earlier label: [bold]{pair.human_label}[/bold]{note}")
        while (answer := console.input("final> ").strip().lower()) not in ("m", "n", "u", "s", "q"):
            console.print("type m, n, u, s or q")
        if answer == "q":
            break
        if answer == "s":
            continue
        reason = console.input("why (optional)> ").strip() or None
        mark_final(pair, CHOICES[answer], reason, by)
        save(pairs)  # after every decision: quitting never loses work
        done += 1
    left = len(todo(pairs, redo=False))
    console.print(f"{done} adjudicated, {left} disagreements still open")
    return {"adjudicated": done, "open": left}
