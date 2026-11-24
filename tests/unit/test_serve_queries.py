import pytest
from pydantic import ValidationError

from poi.serve.api import RouteRequest
from poi.serve.queries import bbox_query, distance_sort, search_query


def test_search_query_combines_relevance_quality_and_distance() -> None:
    q = search_query("upper pines", 37.74, -119.57, 10, ["showers", "pets_allowed"], 7, "rv_park")
    fs = q["function_score"]
    must = fs["query"]["bool"]["must"][0]["multi_match"]
    assert must["fuzziness"] == "AUTO" and "name.autocomplete^2" in must["fields"]
    filters = fs["query"]["bool"]["filter"]
    assert {"term": {"amenities": "showers"}} in filters
    assert {"term": {"months_open": 7}} in filters
    assert {"term": {"category": "rv_park"}} in filters
    assert any("geo_distance" in f for f in filters)
    kinds = {next(iter(f)) for f in fs["functions"]}
    assert kinds == {"field_value_factor", "gauss"}


def test_search_without_text_or_location_matches_all_ranked_by_quality() -> None:
    fs = search_query(None, None, None, None, [], None, None)["function_score"]
    assert fs["query"]["bool"]["must"] == [{"match_all": {}}]
    assert [next(iter(f)) for f in fs["functions"]] == ["field_value_factor"]
    assert distance_sort(None, None) is None


def test_bbox_query_corners() -> None:
    box = bbox_query(37.7, -119.6, 37.8, -119.5)["bool"]["filter"][0]["geo_bounding_box"]
    assert box["location"]["top_left"] == {"lat": 37.8, "lon": -119.6}
    assert box["location"]["bottom_right"] == {"lat": 37.7, "lon": -119.5}


def test_route_request_validation() -> None:
    RouteRequest(route=[(-119.6, 37.7), (-119.5, 37.8)])
    with pytest.raises(ValidationError):
        RouteRequest(route=[(-119.6, 37.7)])  # one point is not a route
    with pytest.raises(ValidationError):
        RouteRequest(route=[(37.7, -119.6), (37.8, -119.5)])  # lat/lon swapped: lat out of range
    with pytest.raises(ValidationError):
        RouteRequest(route=[(-119.6, 37.7), (-119.5, 37.8)], buffer_km=0)
