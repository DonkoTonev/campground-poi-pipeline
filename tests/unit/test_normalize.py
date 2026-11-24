"""Normalization and validation edge cases (brief §7 and §10 "Data"), plus real-data cases."""

from datetime import date

import pytest

from poi.normalize import category, contact, geo, names, season
from poi.normalize.model import Category, OperatorType, Tri
from poi.normalize.pipeline import SourceRow, normalize
from poi.settings import load_region

SIERRA = load_region("sierra")
TODAY = date(2026, 11, 24)
LOWER_PINES = (37.7394658916, -119.566313767)


# --- names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LOWER TWIN LAKES AT BRIDGEPORT", "Lower Twin Lakes at Bridgeport"),
        ("MCGEE CREEK RV PARK", "McGee Creek RV Park"),
        ("KOA CAMPGROUND LOOP B", "KOA Campground Loop B"),
        ("FERN CREEK II", "Fern Creek II"),
        ("ANNETT'S MONO VILLAGE", "Annett's Mono Village"),
        ("O'NEIL FLAT", "O'Neil Flat"),
        ("Upper Pines Campground", "Upper Pines Campground"),  # not shouting: untouched
    ],
)
def test_display_name_smart_title_case(raw: str, expected: str) -> None:
    assert names.display_name(raw)[0] == expected


def test_display_name_strips_page_title_junk_and_whitespace() -> None:
    assert names.display_name("Welcome to  Pine Cliff Resort | Home")[0] == "Pine Cliff Resort"
    assert names.display_name("   ")[0] is None


def test_match_key_expands_abbreviations_for_matching_only() -> None:
    assert names.match_key("Tuolumne Meadows CG") == "tuolumne meadows campground"
    assert names.match_key("TUOLUMNE MEADOWS CAMPGROUND") == "tuolumne meadows campground"
    assert names.match_key("Mt. Whitney Cmpgd") == "mount whitney campground"
    assert names.match_key("N Lake & S Lake") == "north lake and south lake"
    assert names.match_key("Señor Café") == "senor cafe"
    assert names.display_name("Tuolumne Meadows CG")[0] == "Tuolumne Meadows CG"


# --- contact ---------------------------------------------------------------------------


def test_phones_to_e164_with_extension_toll_free_and_odd_formats() -> None:
    assert contact.normalize_phone("(760) 555-0123 ext. 4") == contact.Phone(
        "+17605550123", "4", False
    )
    assert contact.normalize_phone("209/555-0142").e164 == "+12095550142"  # type: ignore[union-attr]
    assert contact.normalize_phone("760-555-0100 OR 760-555-0199").e164 == "+17605550100"  # type: ignore[union-attr]
    toll_free = contact.normalize_phone("1-800-555-0199")  # fictional toll-free
    assert toll_free is not None and toll_free.toll_free
    assert contact.normalize_phone("N/A") is None and contact.is_placeholder("N/A")
    assert contact.normalize_phone("12345") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("www.Example.com/Camp/", "http://example.com/Camp"),
        ("https://www.example.com/?utm_source=x&fbclid=y&page=2#top", "https://example.com?page=2"),
        ("HTTP://EXAMPLE.COM:80/rates", "http://example.com/rates"),
        ("mailto:ranger@example.com", None),
        ("javascript:alert(1)", None),
        ("", None),
    ],
)
def test_canonical_url(raw: str, expected: str | None) -> None:
    assert contact.canonical_url(raw) == expected


def test_social_media_and_email() -> None:
    assert contact.is_social("https://facebook.com/somecampground")
    assert not contact.is_social("https://example.com")
    assert contact.normalize_email(" Info@Example.COM ") == "info@example.com"
    assert contact.normalize_email("n/a") is None


# --- geometry --------------------------------------------------------------------------


def _rules(lat: object, lon: object, claimed: str | None = None) -> set[str]:
    return {f.rule for f in geo.check_coordinates(lat, lon, SIERRA, claimed).flags}


def test_valid_point_in_region_and_state_has_no_flags() -> None:
    result = geo.check_coordinates(*LOWER_PINES, SIERRA, "CA")
    assert result.flags == [] and result.state_code == "CA"
    assert result.precision_m is not None and result.precision_m < 1


@pytest.mark.parametrize(
    ("lat", "lon", "rule"),
    [
        (0, 0, "null_island"),
        (None, None, "missing_coordinates"),
        ("", "", "missing_coordinates"),
        (95.0, -119.5, "out_of_range"),
        (-119.566, 37.739, "swapped_lat_lon"),
        (37.739, 119.566, "positive_longitude"),
        (48.85, 2.35, "outside_north_america"),
        ("37.74", "-119.57", "low_precision"),
        (36.5, -118.5, "outside_region"),
    ],
)
def test_coordinate_problems_are_flagged(lat: object, lon: object, rule: str) -> None:
    assert rule in _rules(lat, lon)


def test_swapped_flag_suggests_fix_but_does_not_apply_it() -> None:
    result = geo.check_coordinates(-119.566, 37.739, SIERRA, None)
    flag = next(f for f in result.flags if f.rule == "swapped_lat_lon")
    assert flag.details["suggested"] == [37.739, -119.566]
    assert result.precision_m is None  # unusable until fixed


def test_point_outside_claimed_state() -> None:
    # Real case: RIDB address is a Nevada forest office, campground is in California.
    assert "outside_claimed_state" in _rules(*LOWER_PINES, claimed="NV")
    assert geo.admin1().code_for("CALIFORNIA") == "CA"
    assert geo.admin1().code_for("Atlantis") is None


# --- category, non-campgrounds, operator -------------------------------------------------


def _classify(
    raw: str, tags: dict[str, str] | None = None, operator: str | None = None, trusted: bool = False
) -> category.Classification:
    return category.classify(raw, names.match_key(raw), tags or {}, operator, None, trusted)


@pytest.mark.parametrize(
    ("raw", "tags", "operator", "reason"),
    [
        ("Berkeley Tuolumne Camp", {"leisure": "summer_camp"}, "City of Berkeley", "organized_camp"),
        ("Lair of the Golden Bear", {"access": "private"}, "University of California, Berkeley", "organized_camp"),
        ("Camp Azuza Senior Camp", {}, None, "organized_camp"),
        ("Camp High Sierra", {"cabins": "10"}, "City of Los Angeles", "organized_camp"),
        ("Tamarack Lodge", {"cabins": "24"}, None, "lodging_only"),
        ("Mi Wok Ranger District Campgrounds", {}, None, "aggregate_facility"),
        ("Campgrounds - Highway 108 Corridor", {}, None, "aggregate_facility"),
        ("Pinecrest Day Use Area", {}, None, "day_use_or_trailhead"),
    ],
)  # fmt: skip
def test_non_campgrounds_with_campground_like_names(
    raw: str, tags: dict[str, str], operator: str | None, reason: str
) -> None:
    result = _classify(raw, tags, operator)
    assert not result.is_campground and result.exclusion_reason == reason
    assert any(f.rule == "non_campground" for f in result.flags)


def test_trailhead_group_camp_and_camp_4_stay_campgrounds() -> None:
    assert _classify("Saddlebag Lake Trailhead Group Camp", trusted=True).is_campground
    camp4 = _classify("Camp 4", trusted=True)
    assert camp4.is_campground and camp4.flags == []  # trusted listing: no name-only noise
    unknown = _classify("Camp Juniper")  # OSM-only "Camp X": kept, but sent to review
    assert unknown.is_campground
    assert [f.rule for f in unknown.flags] == ["possible_non_campground"]


def test_day_use_area_is_excluded_even_from_trusted_listing() -> None:
    assert not _classify("Pinecrest Day Use Area", trusted=True).is_campground


@pytest.mark.parametrize(
    ("raw", "tags", "expected"),
    [
        ("Tuolumne Meadows Horse Camp", {}, Category.HORSE),
        ("Agnew Meadows Group Campground", {"group_only": "yes"}, Category.GROUP),
        ("Merced Lake Backpacker Camp", {"backcountry": "yes"}, Category.BACKCOUNTRY),
        ("Twin Lakes Resort RV Sites", {"tourism": "caravan_site"}, Category.RV_PARK),
        ("Sawmill Walk-in Campground", {}, Category.TENT_WALK_IN),
        ("Goat Meadow Dispersed Campsite", {}, Category.DISPERSED),
        ("Upper Pines Campground", {}, Category.DEVELOPED),
    ],
)
def test_categories(raw: str, tags: dict[str, str], expected: Category) -> None:
    assert _classify(raw, tags).category is expected


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("Bureau of Land Managment", OperatorType.FEDERAL),  # real typo
        ("NFS", OperatorType.FEDERAL),
        ("US National Park service", OperatorType.FEDERAL),
        ("Californa State Parks", OperatorType.STATE),  # real typo
        ("Mono County", OperatorType.COUNTY),
        ("City of Los Angeles", OperatorType.MUNICIPAL),
        ("Bishop Paiute Tribe", OperatorType.TRIBAL),
        ("Thousand Trails", OperatorType.PRIVATE),
        (None, OperatorType.UNKNOWN),
    ],
)
def test_operator_type(operator: str | None, expected: OperatorType) -> None:
    assert category.operator_type(operator) is expected


# --- seasons ---------------------------------------------------------------------------


def test_holiday_anchors_resolve_per_year() -> None:
    assert season.resolve_anchor("memorial_day", 2026) == date(2026, 5, 25)
    assert season.resolve_anchor("memorial_day_weekend", 2026) == date(2026, 5, 22)
    assert season.resolve_anchor("labor_day", 2026) == date(2026, 9, 7)
    assert season.resolve_anchor("labor_day", 2027) == date(2027, 9, 6)
    assert season.resolve_anchor("thanksgiving", 2026) == date(2026, 11, 26)


def test_anchored_and_wrapping_seasons_give_months_open() -> None:
    summer = season.SeasonRange(start_anchor="memorial_day_weekend", end_anchor="labor_day")
    assert season.months_open([summer], 2026) == [5, 6, 7, 8, 9]
    winter = season.SeasonRange(11, 15, 3, 31)  # wraps the new year
    assert season.months_open([winter], 2026) == [1, 2, 3, 11, 12]


@pytest.mark.parametrize(
    ("raw", "months"),
    [("May-Oct", [5, 6, 7, 8, 9, 10]), ("Jun 15-Sep 15: 24/7", [6, 7, 8, 9]), ("24/7", list(range(1, 13)))],
)  # fmt: skip
def test_osm_opening_hours_seasons(raw: str, months: list[int]) -> None:
    parsed = season.parse_osm_opening_hours(raw)
    assert parsed is not None and season.months_open(parsed, 2026) == months


def test_osm_daily_hours_are_not_a_season() -> None:
    assert season.parse_osm_opening_hours("Mo-Fr 08:00-17:00") is None


def _closure(start: str, end: str, name: str = "Seasonal Winter Closure") -> dict[str, object]:
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    return {"name": name, "startDate": start, "endDate": end,
            "exceptionHours": dict.fromkeys(days, "Closed")}  # fmt: skip


def test_nps_recurring_closure_becomes_open_season() -> None:
    hours = [{"standardHours": {"monday": "All Day"},
              "exceptions": [_closure("2026-10-19", "2027-04-21"),
                             _closure("2027-11-18", "2027-11-20", "November Storm Closure")]}]  # fmt: skip
    ranges, flags = season.nps_season(hours, TODAY)
    assert flags == []
    assert len(ranges) == 1
    r = ranges[0]
    assert (r.start_month, r.start_day, r.end_month, r.end_day) == (4, 22, 10, 18)


def test_nps_past_year_closure_is_stale_and_no_closure_is_all_year() -> None:
    hours = [{"standardHours": {"monday": "All Day"},
              "exceptions": [_closure("2024-10-01", "2025-05-01")]}]  # fmt: skip
    _, flags = season.nps_season(hours, TODAY)
    assert [f.rule for f in flags] == ["stale_season"]
    ranges, _ = season.nps_season([{"standardHours": {"monday": "All Day"}}], TODAY)
    assert ranges == [season.ALL_YEAR]


# --- whole pipeline ---------------------------------------------------------------------


def _osm_row(
    tags: dict[str, str], lat: float = LOWER_PINES[0], lon: float = LOWER_PINES[1]
) -> SourceRow:
    return SourceRow(1, "osm", "node/1", "sierra", {"osm_type": "node", "osm_id": 1, "tags": tags}, lat, lon)  # fmt: skip


def test_unknown_amenities_never_default_to_no() -> None:
    rec = normalize(_osm_row({"tourism": "camp_site", "name": "Somewhere"}), SIERRA, TODAY)
    assert set(rec.amenities.values()) == {Tri.UNKNOWN.value}


def test_osm_meters_converted_to_feet_with_flag_and_alt_names_kept() -> None:
    rec = normalize(
        _osm_row({"tourism": "camp_site", "name": "Camp 4", "maxlength": "10",
                  "name_old": "Sunnyside Walk-in Campground", "drinking_water": "no"}),
        SIERRA,
        TODAY,
    )  # fmt: skip
    assert rec.max_rv_length_ft == 33
    assert "unit_converted" in {f.rule for f in rec.flags}
    assert rec.alt_names == ["Sunnyside Walk-in Campground"]
    assert rec.amenities["drinking_water"] == "no"  # explicit "no" is kept
    assert "possible_non_campground" not in {f.rule for f in rec.flags}  # old name says campground


def test_ridb_record_from_fixture() -> None:
    from conftest import load_fixture

    payload = next(
        f for f in load_fixture("ridb/facilities.json")["RECDATA"]
        if f["FacilityName"] == "Lower Pines Campground"
    )  # fmt: skip
    rec = normalize(SourceRow(2, "ridb", str(payload["FacilityID"]), "sierra", payload, None, None),
                    SIERRA, TODAY)  # fmt: skip
    assert rec.is_campground and rec.operator_type is OperatorType.FEDERAL
    assert rec.ids["recgov_facility_id"] == str(payload["FacilityID"])
    assert (
        rec.reservation_url == f"https://recreation.gov/camping/campgrounds/{payload['FacilityID']}"
    )
    assert rec.state_code == "CA"
    assert rec.amenities["ada_accessible"] == "unknown"  # RIDB "N" is not evidence of "no"
