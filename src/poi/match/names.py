"""Core names and discriminator tokens.

"Upper Pines Campground" and "Lower Pines Campground" share the core name "pines" but carry
conflicting position discriminators. "Tuolumne Meadows Horse Camp" carries a type
discriminator that "Tuolumne Meadows Campground" lacks: a different facility, not a variant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GENERIC = frozenset(
    {"campground", "camp", "campsite", "campsites", "site", "sites", "area", "recreation",
     "rec", "the", "and", "at", "of", "facility", "family", "park", "unit"}
)  # fmt: skip
POSITION = {  # qualifiers that name a *different* place next to a namesake
    w: w
    for w in (
        "upper", "lower", "middle", "north", "south", "east", "west", "inner", "outer",
        "old", "new",
    )
}  # fmt: skip
TYPE = {
    "group": "group",
    "horse": "horse",
    "equestrian": "horse",
    "stock": "horse",
    "overflow": "overflow",
    "walkin": "walkin",
    "backpacker": "backpacker",
    "backpackers": "backpacker",
}
# Soft types: "Pine Cliff Resort RV and Campground" is the same place as "Pine Cliff Resort".
SOFT = {"rv": "rv", "tent": "tent"}
ROMAN = re.compile(r"^(ii|iii|iv|v|vi|vii|viii|ix|x)$")


@dataclass(frozen=True)
class NameParts:
    core: str
    position: frozenset[str]
    type: frozenset[str]
    unit: frozenset[str]  # loop letters/numbers, roman numerals
    soft: frozenset[str]  # rv / tent


def parts(match_name: str | None) -> NameParts:
    """Split a matching key (see normalize.names.match_key) into core and discriminators."""
    text = (match_name or "").replace("walk in", "walkin").replace("hike in", "walkin")
    tokens = text.split()
    position: set[str] = set()
    kind: set[str] = set()
    unit: set[str] = set()
    soft: set[str] = set()
    core: list[str] = []
    for i, tok in enumerate(tokens):
        prev = tokens[i - 1] if i else ""
        if tok in POSITION:
            position.add(POSITION[tok])
        elif tok in TYPE:
            kind.add(TYPE[tok])
        elif tok in SOFT:
            soft.add(SOFT[tok])
        elif prev == "loop" or ROMAN.match(tok):
            unit.add(tok)
        elif tok == "loop" or tok in GENERIC:
            continue
        else:
            core.append(tok)
    return NameParts(
        " ".join(core), frozenset(position), frozenset(kind), frozenset(unit), frozenset(soft)
    )


@dataclass(frozen=True)
class Discriminators:
    conflict: list[str]
    one_sided: list[str]


def compare(a: NameParts, b: NameParts) -> Discriminators:
    """Conflicting discriminators are strong non-match evidence. A hard type present on one
    side only (horse camp vs campground) is also a conflict; a position, unit or soft type on
    one side only ("Pines" vs "North Pines") is weak evidence."""
    conflict: list[str] = []
    one_sided: list[str] = []
    families = (
        ("position", a.position, b.position),
        ("unit", a.unit, b.unit),
        ("soft_type", a.soft, b.soft),
    )
    for family, x, y in families:
        if x and y and x != y:
            conflict.append(family)
        elif x != y:
            one_sided.append(family)
    if a.type != b.type:
        conflict.append("type")
    return Discriminators(conflict, one_sided)
