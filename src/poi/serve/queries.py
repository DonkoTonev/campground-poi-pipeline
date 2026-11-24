"""Elasticsearch query builders (pure functions, unit-tested without a cluster)."""

from __future__ import annotations

from typing import Any

DISTANCE_SCALE_KM = 25  # distance decay: a campground 25 km away scores half


def search_query(
    q: str | None,
    lat: float | None,
    lon: float | None,
    radius_km: float | None,
    amenities: list[str],
    open_in_month: int | None,
    category: str | None,
) -> dict[str, Any]:
    """Relevance (fuzzy name and alt names, prefix autocomplete) * quality * distance decay."""
    filters: list[dict[str, Any]] = [{"term": {"amenities": a}} for a in amenities]
    if open_in_month is not None:
        filters.append({"term": {"months_open": open_in_month}})
    if category:
        filters.append({"term": {"category": category}})
    if lat is not None and lon is not None and radius_km is not None:
        filters.append(
            {"geo_distance": {"distance": f"{radius_km}km", "location": {"lat": lat, "lon": lon}}}
        )
    must: list[dict[str, Any]] = []
    if q:
        must.append(
            {
                "multi_match": {
                    "query": q,
                    "fields": ["name^3", "name.autocomplete^2", "alt_names"],
                    "fuzziness": "AUTO",
                    "prefix_length": 1,
                    "operator": "and",
                }
            }
        )
    functions: list[dict[str, Any]] = [
        {"field_value_factor": {"field": "quality_score", "missing": 0.5, "modifier": "none"}}
    ]
    if lat is not None and lon is not None:
        functions.append(
            {
                "gauss": {
                    "location": {
                        "origin": {"lat": lat, "lon": lon},
                        "scale": f"{DISTANCE_SCALE_KM}km",
                        "decay": 0.5,
                    }
                }
            }
        )
    return {
        "function_score": {
            "query": {"bool": {"must": must or [{"match_all": {}}], "filter": filters}},
            "functions": functions,
            "score_mode": "multiply",
            "boost_mode": "multiply",
        }
    }


def distance_sort(lat: float | None, lon: float | None) -> list[dict[str, Any]] | None:
    if lat is None or lon is None:
        return None
    return [
        {"_score": "desc"},
        {"_geo_distance": {"location": {"lat": lat, "lon": lon}, "order": "asc", "unit": "km"}},
    ]


def bbox_query(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> dict[str, Any]:
    return {
        "bool": {
            "filter": [
                {
                    "geo_bounding_box": {
                        "location": {
                            "top_left": {"lat": max_lat, "lon": min_lon},
                            "bottom_right": {"lat": min_lat, "lon": max_lon},
                        }
                    }
                }
            ]
        }
    }
