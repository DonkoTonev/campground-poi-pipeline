"""Stable entity ids across runs.

New clusters are mapped to previous entities by member overlap (record refs, not database
ids). The previous entity with the largest overlap keeps its id; ties go to the older id.
- merge: two old entities end up in one cluster: the loser redirects to the winner;
- split: one old entity spreads over several clusters: the largest part keeps the id, the
  others get new ids (no redirect: the old id still exists);
- retire: an old entity whose records all left the campground set keeps its id, status
  `retired`, never deleted.
Consumers that saved an id therefore always land on a live entity (directly or via redirect).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Assignment:
    cluster_ids: dict[int, str]  # cluster index -> entity id
    created: list[str] = field(default_factory=list)
    redirects: dict[str, str] = field(default_factory=dict)  # old id -> new id
    retired: list[str] = field(default_factory=list)


def assign(
    previous: dict[str, frozenset[str]],
    clusters: list[frozenset[str]],
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> Assignment:
    """`previous`: active entity id -> member refs. `clusters`: member refs per new cluster."""
    owner = {ref: eid for eid, refs in previous.items() for ref in refs}
    # Candidate (overlap, entity, cluster) triples, strongest first; ids sort for determinism.
    triples = sorted(
        (
            (len(refs & previous[eid]), eid, i)
            for i, refs in enumerate(clusters)
            for eid in {owner[r] for r in refs if r in owner}
        ),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    result = Assignment({})
    taken: set[str] = set()
    for _, eid, i in triples:
        if i in result.cluster_ids or eid in taken:
            continue
        result.cluster_ids[i] = eid
        taken.add(eid)
    for i in range(len(clusters)):
        if i not in result.cluster_ids:
            result.cluster_ids[i] = new_id()
            result.created.append(result.cluster_ids[i])

    for eid, refs in previous.items():
        if eid in taken:
            continue
        # Where did its members go? Redirect to the entity holding most of them.
        destinations = [
            result.cluster_ids[i] for i, members in enumerate(clusters) for _ in refs & members
        ]
        if destinations:
            winner = max(sorted(set(destinations)), key=destinations.count)
            result.redirects[eid] = winner
        else:
            result.retired.append(eid)
    return result
