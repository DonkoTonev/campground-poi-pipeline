"""Constrained clustering of accepted match edges, plus parent/child hierarchy.

Edges are added strongest first (union-find). An edge that would put a cannot-link pair in
one cluster, or stretch a cluster beyond the maximum diameter, is not added; it goes to
review instead. This is the "split on the weakest edge" rule applied up front: in a chain
A~B~C with A and C incompatible, the weaker of the two edges is the one left out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from poi.match.scoring import MatchRecord, Scored, cannot_link, shares_id

EARTH_RADIUS_M = 6_371_000.0
SUBTYPE_CATEGORIES = frozenset({"group", "horse"})


def haversine_m(a: MatchRecord, b: MatchRecord) -> float | None:
    if None in (a.lat, a.lon, b.lat, b.lon):
        return None
    p1, p2 = math.radians(a.lat), math.radians(b.lat)  # type: ignore[arg-type]
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)  # type: ignore[operator]
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


class _UnionFind:
    def __init__(self, ids: list[int]) -> None:
        self.parent = {i: i for i in ids}
        self.members: dict[int, list[int]] = {i: [i] for i in ids}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra] += self.members.pop(rb)


@dataclass(frozen=True)
class Blocked:
    scored: Scored
    reason: str


@dataclass(frozen=True)
class Clustering:
    clusters: dict[int, list[int]]  # cluster key (smallest member id) -> record ids
    blocked: list[Blocked]  # accepted edges the constraints rejected: review items


def cluster(
    records: dict[int, MatchRecord],
    scored: list[Scored],
    max_diameter_m: float,
    strong_non_match: float,
    manual_non_match: frozenset[frozenset[int]] = frozenset(),
) -> Clustering:
    """Edges marked `reason == "override"` are manual decisions: they go first and only
    manual non-match decisions can stop them. `manual_non_match` pairs never share a cluster."""
    strong_negative = {
        frozenset((s.candidate.a.id, s.candidate.b.id))
        for s in scored
        if s.score <= strong_non_match
    }
    uf = _UnionFind(list(records))
    blocked: list[Blocked] = []
    # Records joined across more than the diameter by a shared id: one of their points is
    # wrong, so they are left out of later diameter checks.
    untrusted_position: set[int] = set()
    edges = sorted(
        (s for s in scored if s.decision == "match"),
        key=lambda s: (s.reason != "override", -s.score),
    )
    for edge in edges:
        ra, rb = uf.find(edge.candidate.a.id), uf.find(edge.candidate.b.id)
        if ra == rb:
            continue
        # An edge backed by a shared authoritative id (recreation.gov, NPS, wikidata) is not
        # a fuzzy chain link, so the diameter guard does not apply: a far-away point on one
        # side is a coordinate error to resolve in survivorship, not a different campground.
        a, b = edge.candidate.a, edge.candidate.b
        by_id = shares_id(a, b)
        if edge.reason == "override":
            reason = _manual_violation(uf.members[ra], uf.members[rb], manual_non_match)
        else:
            reason = _violation(
                records,
                uf.members[ra],
                uf.members[rb],
                strong_negative | manual_non_match,
                None if by_id else max_diameter_m,
                untrusted_position,
            )
        if reason:
            blocked.append(Blocked(edge, reason))
            continue
        if by_id and (edge.candidate.distance_m or 0.0) > max_diameter_m:
            untrusted_position.update((a.id, b.id))
        uf.union(ra, rb)
    clusters = {min(m): sorted(m) for m in uf.members.values()}
    return Clustering(clusters, blocked)


def _manual_violation(
    left: list[int], right: list[int], manual_non_match: frozenset[frozenset[int]]
) -> str | None:
    if any(frozenset((i, j)) in manual_non_match for i in left for j in right):
        return "manual_non_match_in_cluster"
    return None


def _violation(
    records: dict[int, MatchRecord],
    left: list[int],
    right: list[int],
    strong_negative: set[frozenset[int]],
    max_diameter_m: float | None,
    untrusted_position: set[int],
) -> str | None:
    for i in left:
        for j in right:
            a, b = records[i], records[j]
            if reason := cannot_link(a, b):
                return reason
            if frozenset((i, j)) in strong_negative:
                return "strong_non_match_in_cluster"
            if max_diameter_m is None or {i, j} & untrusted_position:
                continue
            d = haversine_m(a, b)
            slack = max(a.precision_m or 0.0, b.precision_m or 0.0)
            if d is not None and d - slack > max_diameter_m:
                return "max_diameter"
    return None


@dataclass(frozen=True)
class Relation:
    child: int
    parent: int
    relation: str


def hierarchy(scored: list[Scored], clusters: dict[int, list[int]]) -> list[Relation]:
    """A group/horse camp, a loop, or an unnamed site lying inside another campground's
    polygon becomes its child, not a duplicate. Only between different clusters."""
    cluster_of = {rid: key for key, members in clusters.items() for rid in members}
    out: dict[int, Relation] = {}
    for s in scored:
        c = s.candidate
        for child, parent, inside in ((c.b, c.a, c.a_contains_b), (c.a, c.b, c.b_contains_a)):
            if not inside or cluster_of[child.id] == cluster_of[parent.id]:
                continue
            if child.category in SUBTYPE_CATEGORIES or child.parts.unit:
                kind = "sub_area"
            elif not child.name:
                kind = "unnamed_part"
            else:
                continue
            out.setdefault(child.id, Relation(child.id, parent.id, kind))
    return sorted(out.values(), key=lambda r: r.child)
