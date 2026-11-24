"""Entity quality score in [0, 1]: completeness, corroboration, freshness, validity.

Used for search ranking and in the quality report. Components are stored with the score so
a low score can be explained ("no season, single source").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QualityInputs:
    golden: dict[str, Any]
    n_sources: int
    missed_runs: int  # max over member records
    stale_season: bool
    errors: int
    warnings: int


def _present(golden: dict[str, Any], key: str) -> bool:
    if key == "contact":
        return bool(golden.get("phone") or golden.get("website"))
    if key == "reservations":
        return golden.get("reservable") is not None or bool(golden.get("reservation_url"))
    if key == "amenities":
        known = [v for v in (golden.get("amenities") or {}).values() if v != "unknown"]
        return len(known) >= 3
    return golden.get(key) not in (None, [], {})


def score(
    inputs: QualityInputs, weights: dict[str, float], key_fields: list[str]
) -> dict[str, Any]:
    completeness = sum(_present(inputs.golden, k) for k in key_fields) / len(key_fields)
    corroboration = min(1.0, (inputs.n_sources - 1) / 2)
    freshness = 1.0
    if inputs.missed_runs:
        freshness -= min(0.6, 0.2 * inputs.missed_runs)
    if inputs.stale_season:
        freshness -= 0.3
    validity = max(0.0, 1.0 - 0.25 * inputs.errors - 0.05 * inputs.warnings)
    components = {
        "completeness": round(completeness, 3),
        "corroboration": round(corroboration, 3),
        "freshness": round(max(freshness, 0.0), 3),
        "validity": round(validity, 3),
    }
    total = sum(weights[k] * v for k, v in components.items())
    return {
        "score": round(total, 3),
        **components,
        "missing": [k for k in key_fields if not _present(inputs.golden, k)],
    }
