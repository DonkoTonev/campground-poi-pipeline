"""Field-level survivorship: pick each golden value from the cluster's records, explain why.

Equal candidate values form a group. Group strength = highest source trust in the group +
`corroboration_bonus` per additional independent source agreeing. The strongest group wins;
all other candidates are kept as alternatives with provenance. A disagreement where the
losing side is also trusted is a conflict; on high-impact fields (reservability, season)
it is flagged for review instead of being decided silently.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Candidate:
    value: Any
    source: str
    record_ref: str


@dataclass
class FieldResult:
    value: Any
    source: str | None
    record_ref: str | None
    reason: str
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    conflict: bool = False


@dataclass(frozen=True)
class Rules:
    raw: dict[str, Any]

    def trust(self, fld: str, source: str) -> float:
        spec = self.raw["fields"].get(fld, {})
        table = spec.get("trust") or self.raw["default_trust"]
        return float(table.get(source, self.raw["default_trust"].get(source, 0.1)))

    def spec(self, fld: str) -> dict[str, Any]:
        return dict(self.raw["fields"].get(fld, {}))

    @property
    def bonus(self) -> float:
        return float(self.raw["corroboration_bonus"])

    @property
    def conflict_min_trust(self) -> float:
        return float(self.raw["conflict_min_trust"])


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def choose(fld: str, candidates: list[Candidate], rules: Rules) -> FieldResult | None:
    """Generic scalar survivorship. Returns None when no record has a value."""
    cands = [c for c in candidates if c.value is not None]
    if not cands:
        return None
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        groups[_key(c.value)].append(c)

    def strength(members: list[Candidate]) -> float:
        sources = {c.source for c in members}
        return max(rules.trust(fld, s) for s in sources) + rules.bonus * (len(sources) - 1)

    ranked = sorted(groups.values(), key=lambda g: (-strength(g), _key(g[0].value)))
    winner = ranked[0]
    best = max(winner, key=lambda c: rules.trust(fld, c.source))
    sources = sorted({c.source for c in winner})
    reason = f"highest trust ({best.source} {rules.trust(fld, best.source):g})"
    if len(sources) > 1:
        reason += f", corroborated by {', '.join(sources)}"
    losers = [c for g in ranked[1:] for c in g]
    conflict = any(rules.trust(fld, c.source) >= rules.conflict_min_trust for c in losers)
    return FieldResult(
        value=best.value,
        source=best.source,
        record_ref=best.record_ref,
        reason=reason,
        alternatives=[
            {"value": c.value, "source": c.source, "record": c.record_ref}
            for c in cands
            if c is not best
        ],
        conflict=conflict,
    )


@dataclass(frozen=True)
class NameCandidate:
    display: str
    match_name: str
    source: str
    record_ref: str
    alt_names: tuple[str, ...] = ()


def choose_name(
    candidates: list[NameCandidate], rules: Rules
) -> tuple[FieldResult | None, list[str]]:
    """Majority of normalized names weighted by trust; display form from the most trusted
    record in the winning group. Every other spelling becomes an alt name (search uses them)."""
    if not candidates:
        return None, sorted({a for c in candidates for a in c.alt_names})
    votes: dict[str, float] = defaultdict(float)
    for c in candidates:
        votes[c.match_name] += rules.trust("name", c.source)
    winning = max(sorted(votes), key=lambda k: votes[k])
    best = max(
        (c for c in candidates if c.match_name == winning),
        key=lambda c: rules.trust("name", c.source),
    )
    alts = {c.display for c in candidates} | {a for c in candidates for a in c.alt_names}
    alts.discard(best.display)
    result = FieldResult(
        value=best.display,
        source=best.source,
        record_ref=best.record_ref,
        reason=f"weighted vote {votes[winning]:.2f} of {sum(votes.values()):.2f}; "
        f"display form from {best.source}",
        alternatives=[
            {"value": c.display, "source": c.source, "record": c.record_ref}
            for c in candidates
            if c is not best
        ],
    )
    return result, sorted(alts, key=str.lower)


@dataclass(frozen=True)
class PointCandidate:
    lat: float
    lon: float
    has_shape: bool
    source: str
    record_ref: str


def _metres(a: PointCandidate, b: PointCandidate) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def choose_geometry(candidates: list[PointCandidate], rules: Rules) -> FieldResult | None:
    """A mapped polygon beats any point (entrance, office or centroid); then source trust.
    Member points far from the chosen one are a conflict: some source has a bad coordinate."""
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.has_shape, rules.trust("geometry", c.source)))
    far = [
        {"source": c.source, "record": c.record_ref, "distance_m": round(_metres(best, c))}
        for c in candidates
        if c is not best and _metres(best, c) > rules.spec("geometry").get("conflict_m", 1000)
    ]
    reason = f"{'polygon' if best.has_shape else 'point'} from {best.source}"
    return FieldResult(
        value={"lat": best.lat, "lon": best.lon},
        source=best.source,
        record_ref=best.record_ref,
        reason=reason,
        alternatives=[
            {
                "value": {"lat": c.lat, "lon": c.lon},
                "source": c.source,
                "record": c.record_ref,
                "distance_m": round(_metres(best, c)),
            }
            for c in candidates
            if c is not best
        ],
        conflict=bool(far),
    )


def choose_amenities(
    per_record: list[tuple[str, str, dict[str, str]]], rules: Rules
) -> tuple[dict[str, str], dict[str, FieldResult]]:
    """Per amenity, only known values compete ("unknown" never beats a stated value).
    Returns (amenity -> value, amenity -> result for amenities that had any known value)."""
    amenities = sorted({a for _, _, values in per_record for a in values})
    golden: dict[str, str] = {}
    results: dict[str, FieldResult] = {}
    for amenity in amenities:
        cands = [
            Candidate(values[amenity], source, ref)
            for source, ref, values in per_record
            if values.get(amenity, "unknown") != "unknown"
        ]
        result = choose("amenities", cands, rules)
        if result is None:
            golden[amenity] = "unknown"
            continue
        golden[amenity] = result.value
        results[amenity] = result
    return golden, results
