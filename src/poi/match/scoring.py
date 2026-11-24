"""Pairwise features and Fellegi-Sunter-style log-odds scoring.

score = prior + sum(contributions). Every contribution is returned by name, so a decision
can be explained ("+5.0 name, +3.0 distance, -6.0 discriminator conflict: position").
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from rapidfuzz import fuzz

from poi.match import names
from poi.settings import Settings, load_yaml

SHARED_ID_KINDS = ("recgov_facility_id", "nps_id", "wikidata")
UNIQUE_ID_SOURCES = frozenset({"ridb", "nps"})  # one facility per id: distinct ids = distinct
SUBTYPE_CATEGORIES = frozenset({"horse", "group"})


@dataclass(frozen=True)
class MatchRecord:
    id: int
    source: str
    source_id: str
    name: str | None
    match_name: str | None
    category: str
    lat: float | None = None
    lon: float | None = None
    precision_m: float | None = None
    ids: dict[str, str] = field(default_factory=dict)
    phone: str | None = None
    website: str | None = None
    alt_match_names: tuple[str, ...] = ()
    has_shape: bool = False

    @property
    def parts(self) -> names.NameParts:
        return _parts(self.match_name)


@lru_cache(maxsize=4096)
def _parts(match_name: str | None) -> names.NameParts:
    return names.parts(match_name)


@dataclass(frozen=True)
class Candidate:
    a: MatchRecord
    b: MatchRecord
    distance_m: float | None  # polygon-aware: 0 when a point lies inside the other's polygon
    blocked_by: tuple[str, ...] = ()
    a_contains_b: bool = False
    b_contains_a: bool = False


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    contributions: dict[str, float]
    evidence: dict[str, Any]
    decision: str  # match | review | non_match
    reason: str


class Frequencies:
    """How many records share each phone / website: shared values are weak evidence."""

    def __init__(self, records: Iterable[MatchRecord]) -> None:
        records = list(records)
        self.n = max(len(records), 2)
        self.phone = Counter(r.phone for r in records if r.phone)
        self.website = Counter(r.website for r in records if r.website)

    def weight(self, kind: str, value: str, max_weight: float) -> float:
        """IDF scaled to [0, max], squared so a value shared by many records counts little."""
        freq = max(getattr(self, kind)[value], 1)
        ratio = math.log(self.n / freq) / math.log(self.n)
        return round(max_weight * max(ratio, 0.0) ** 2, 3)


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]


def load_config(settings: Settings | None = None) -> Config:
    return Config(load_yaml("matching.yaml", settings))


def _binned(value: float, bins: list[list[Any]], upper: bool) -> float:
    """`upper`: bins are [upper_bound, contribution] (null = infinity); else [lower_bound, c]."""
    for bound, contribution in bins:
        if upper and (bound is None or value <= bound):
            return float(contribution)
        if not upper and value >= bound:
            return float(contribution)
    raise ValueError(f"no bin for {value}")


def name_similarity(a: MatchRecord, b: MatchRecord) -> tuple[float | None, bool]:
    """Best core-name similarity (0-100) over names and former names; second value tells
    whether the best score needed a former/alternative name (a possible rename)."""
    if not a.match_name or not b.match_name:
        return None, False

    def sim(x: str, y: str) -> float:
        cx, cy = names.parts(x).core, names.parts(y).core
        if not cx or not cy:  # name is only generic words: compare full keys
            cx, cy = x, y
        return float(fuzz.token_sort_ratio(cx, cy))

    primary = sim(a.match_name, b.match_name)
    alts = [
        sim(x, y)
        for x in (a.match_name, *a.alt_match_names)
        for y in (b.match_name, *b.alt_match_names)
        if (x, y) != (a.match_name, b.match_name)
    ]
    best_alt = max(alts, default=0.0)
    return (best_alt, True) if best_alt > primary else (primary, False)


def score_pair(c: Candidate, freq: Frequencies, cfg: Config) -> Scored:
    a, b = c.a, c.b
    contributions: dict[str, float] = {"prior": float(cfg["prior"])}
    evidence: dict[str, Any] = {"blocked_by": list(c.blocked_by)}

    # Distance: evidence, never a rule. Low-precision coordinates get the benefit of the doubt.
    if c.distance_m is None:
        contributions["distance"] = float(cfg["unknown_distance"])
    else:
        slack = max(a.precision_m or 0.0, b.precision_m or 0.0)
        effective = max(0.0, c.distance_m - slack) if slack > 100 else c.distance_m
        contributions["distance"] = _binned(effective, cfg["distance"], upper=True)
        evidence["distance_m"] = round(c.distance_m, 1)

    similarity, via_alt = name_similarity(a, b)
    if similarity is None:
        contributions["name"] = float(cfg["missing_name"])
    else:
        contributions["name"] = _binned(similarity, cfg["name"], upper=False)
        evidence["name_similarity"] = similarity
        if via_alt:
            evidence["name_via_alt"] = True

    disc = names.compare(a.parts, b.parts)
    if disc.conflict and shares_id(a, b):
        # Same authoritative id: the conflict is likely a rename or naming variant. Keep it as
        # evidence, weigh it like a one-sided discriminator.
        contributions["discriminator_conflict"] = float(cfg["discriminators"]["one_sided"])
        evidence["discriminator_conflict"] = disc.conflict
    elif disc.conflict:
        contributions["discriminator_conflict"] = float(cfg["discriminators"]["conflict"])
        evidence["discriminator_conflict"] = disc.conflict
    elif disc.one_sided:
        contributions["discriminator_one_sided"] = float(cfg["discriminators"]["one_sided"])
        evidence["discriminator_one_sided"] = disc.one_sided

    for kind in SHARED_ID_KINDS:
        x, y = a.ids.get(kind), b.ids.get(kind)
        if x and y:
            key = "shared_id" if x == y else "different_id"
            value = cfg["shared_id"]["same" if x == y else "different"]
            # Several id kinds agreeing is still one piece of evidence: keep the strongest.
            if abs(value) > abs(contributions.get(key, 0.0)):
                contributions[key] = float(value)
            evidence.setdefault("ids", []).append({kind: [x, y]})

    if a.phone and a.phone == b.phone:
        contributions["phone"] = freq.weight("phone", a.phone, cfg["phone"]["max"])
        evidence["phone_shared_by"] = freq.phone[a.phone]
    if a.website and a.website == b.website:
        contributions["website"] = freq.weight("website", a.website, cfg["website"]["max"])
        evidence["website_shared_by"] = freq.website[a.website]

    if a.category == b.category:
        contributions["category"] = float(cfg["category"]["same"])
    elif "type" not in disc.conflict and ({a.category, b.category} & SUBTYPE_CATEGORIES):
        contributions["category"] = float(cfg["category"]["incompatible"])

    total = round(sum(contributions.values()), 3)
    decision, reason = decide(total, evidence, cfg)
    return Scored(c, total, contributions, evidence, decision, reason)


def decide(score: float, evidence: dict[str, Any], cfg: Config) -> tuple[str, str]:
    thresholds = cfg["thresholds"]
    if score >= thresholds["match"]:
        if evidence.get("name_via_alt") and cfg["alt_name_caps_at_review"]:
            return "review", "possible_rename"
        return "match", "score"
    if score < thresholds["non_match"]:
        return "non_match", "score"
    return "review", "gray_zone"


def shares_id(a: MatchRecord, b: MatchRecord) -> bool:
    return any(a.ids.get(k) and a.ids.get(k) == b.ids.get(k) for k in SHARED_ID_KINDS)


def cannot_link(a: MatchRecord, b: MatchRecord) -> str | None:
    """Hard constraints that no score can override.

    Two different ids from a source that guarantees uniqueness are always distinct places.
    Conflicting name discriminators are too, unless both records carry the same authoritative
    id: then the id wins (a renamed campground can carry a different discriminator).
    """
    if a.source == b.source and a.source in UNIQUE_ID_SOURCES and a.source_id != b.source_id:
        return f"distinct_{a.source}_ids"
    if names.compare(a.parts, b.parts).conflict and not shares_id(a, b):
        return "discriminator_conflict"
    return None
