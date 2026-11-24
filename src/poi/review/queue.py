"""`poi review queue`: work through gray-zone pairs and field conflicts.

Every decision is written to data/overrides.yaml (versioned, applied on every run), so a
manual fix becomes a rule instead of a one-off database edit. Re-run `poi match` and
`poi resolve` afterwards to apply the decisions.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from sqlalchemy import Connection, text

from poi.resolve import overrides
from poi.review.label import record_row, render_pair

CONFLICT_RULES = ("high_impact_conflict", "geometry_conflict", "amenity_conflict")


def pending_pairs(conn: Connection, region: str) -> list[Any]:
    return list(
        conn.execute(
            text(
                """
                SELECT a.source || ':' || a.source_id AS a_ref,
                       b.source || ':' || b.source_id AS b_ref,
                       d.score, d.reason, d.contributions
                FROM match_decisions d
                JOIN poi_records a ON a.id = d.a_id JOIN poi_records b ON b.id = d.b_id
                WHERE d.region = :r AND d.decision = 'review' ORDER BY d.score DESC
                """
            ),
            {"r": region},
        )
    )


def pending_conflicts(conn: Connection, region: str) -> list[Any]:
    return list(
        conn.execute(
            text(
                """
                SELECT e.id::text AS entity_id, e.name, q.field, q.rule, q.details, f.record_ref
                FROM quality_flags q JOIN entities e ON e.id = q.entity_id
                LEFT JOIN entity_fields f ON f.entity_id = e.id AND f.field = q.field
                WHERE e.region = :r AND q.stage = 'resolve' AND q.rule = ANY(:rules)
                ORDER BY q.rule, e.name
                """
            ),
            {"r": region, "rules": list(CONFLICT_RULES)},
        )
    )


def _ref(ref: str) -> dict[str, str]:
    source, source_id = ref.split(":", 1)
    return {"source": source, "source_id": source_id}


def _ask(console: Console, prompt: str, valid: set[str]) -> str:
    while (answer := console.input(prompt).strip().lower()) not in valid:
        console.print(f"type one of: {', '.join(sorted(valid))}")
    return answer


def run(conn: Connection, region: str) -> dict[str, int]:
    console = Console()
    ovr = overrides.load()
    decided = 0
    pairs, conflicts = pending_pairs(conn, region), pending_conflicts(conn, region)
    console.print(f"{len(pairs)} pairs and {len(conflicts)} field conflicts to review.")

    for i, p in enumerate(pairs, start=1):
        a, b = record_row(conn, _ref(p.a_ref)), record_row(conn, _ref(p.b_ref))
        if a is None or b is None:
            continue
        render_pair(
            console,
            f"Pair {i}/{len(pairs)} · score {p.score:g} · {p.reason}",
            p.a_ref,
            p.b_ref,
            a,
            b,
        )
        console.print(
            "contributions: "
            + ", ".join(
                f"{k} {v:+g}"
                for k, v in sorted(p.contributions.items(), key=lambda kv: -abs(kv[1]))
            )
        )
        answer = _ask(console, r"\[m]atch \[n]on-match \[s]kip \[q]uit> ", {"m", "n", "s", "q"})
        if answer == "q":
            overrides.save(ovr)
            return {"decided": decided}
        if answer in ("m", "n"):
            reason = console.input("reason> ").strip() or "reviewed"
            decision = "match" if answer == "m" else "non_match"
            overrides.add_match(ovr, p.a_ref, p.b_ref, decision, reason)
            overrides.save(ovr)
            decided += 1

    for i, c in enumerate(conflicts, start=1):
        details: dict[str, Any] = c.details
        field = f"amenities.{details['amenity']}" if c.field == "amenities" else c.field
        options = [{"value": details.get("chosen"), "record": c.record_ref, "source": "current"}]
        options += details.get("alternatives") or []
        console.print(
            f"\n[bold]Conflict {i}/{len(conflicts)}[/bold] · {c.name} · {field} ({c.rule})"
        )
        for n, opt in enumerate(options, start=1):
            console.print(
                f"  {n}. {json.dumps(opt.get('value'), default=str)} "
                f"({opt.get('source')}, {opt.get('record')})"
            )
        valid = {str(n) for n in range(1, len(options) + 1)} | {"s", "q"}
        answer = _ask(console, "keep which value? number, [s]kip, [q]uit> ", valid)
        if answer == "q":
            break
        if answer == "s":
            continue
        chosen = options[int(answer) - 1]
        reason = console.input("reason> ").strip() or "reviewed conflict"
        anchor = c.record_ref or chosen["record"]
        overrides.add_field(ovr, anchor, field, chosen["value"], reason)
        overrides.save(ovr)
        decided += 1
    console.print(
        f"{decided} decisions written to {overrides.OVERRIDES_PATH.name}; "
        "run `poi match` and `poi resolve` to apply them."
    )
    return {"decided": decided}
