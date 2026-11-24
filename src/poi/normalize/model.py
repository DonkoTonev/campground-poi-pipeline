"""Canonical POI model shared by all sources (and later by LLM-extracted records)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Category(StrEnum):
    DEVELOPED = "developed_campground"
    RV_PARK = "rv_park"
    TENT_WALK_IN = "tent_walk_in"
    GROUP = "group"
    HORSE = "horse"
    DISPERSED = "dispersed"
    CABINS_GLAMPING = "cabins_glamping"
    BACKCOUNTRY = "backcountry"
    OTHER = "other"


class OperatorType(StrEnum):
    FEDERAL = "federal"
    STATE = "state"
    COUNTY = "county"
    MUNICIPAL = "municipal"
    TRIBAL = "tribal"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class Tri(StrEnum):
    """Tri-state (plus nuance) amenity value. Absence of evidence is UNKNOWN, never NO."""

    YES = "yes"
    NO = "no"
    SEASONAL = "seasonal"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


AMENITIES = (
    "drinking_water",
    "flush_toilets",
    "vault_toilets",
    "showers",
    "electric_hookups",
    "water_hookups",
    "sewer_hookups",
    "dump_station",
    "laundry",
    "camp_store",
    "wifi",
    "fire_rings",
    "picnic_tables",
    "pets_allowed",
    "generators_allowed",
    "ada_accessible",
)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Flag:
    field: str
    rule: str
    severity: Severity
    details: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclass
class Draft:
    """Raw values a source mapper extracted, named canonically but not yet normalized."""

    name: str | None = None
    alt_names: list[str] = dataclasses.field(default_factory=list)
    raw_lat: Any = None  # as the source sent it (string or number), for precision checks
    raw_lon: Any = None
    phone: str | None = None
    website: str | None = None
    reservation_url: str | None = None
    email: str | None = None
    address: dict[str, str] = dataclasses.field(default_factory=dict)
    claimed_state: str | None = None
    operator_name: str | None = None
    operator_type_hint: OperatorType | None = None
    category_hint: Category | None = None
    trusted_campground: bool = False  # federal campground listings: never reclassify by name
    ids: dict[str, str] = dataclasses.field(default_factory=dict)
    amenities: dict[str, Tri] = dataclasses.field(default_factory=dict)
    num_sites: int | None = None
    sites_by_type: dict[str, int] = dataclasses.field(default_factory=dict)
    max_rv_length: tuple[float, str] | None = None  # (value, unit) with unit in {"ft", "m"}
    reservable: bool | None = None
    first_come_first_served: bool | None = None
    price_min: float | None = None
    price_max: float | None = None
    currency: str | None = None
    season_raw: Any = None  # source-specific; parsed by the source's season adapter
    season_note: str | None = None
    tags: dict[str, str] = dataclasses.field(default_factory=dict)  # OSM tags, for rule evidence
    flags: list[Flag] = dataclasses.field(default_factory=list)  # flags raised while mapping


@dataclass
class CanonicalRecord:
    source: str
    source_id: str
    source_record_id: int
    region: str
    name: str | None
    match_name: str | None
    alt_names: list[str]
    category: Category
    is_campground: bool
    exclusion_reason: str | None
    operator_type: OperatorType
    operator_name: str | None
    phone: str | None
    phone_ext: str | None
    website: str | None
    reservation_url: str | None
    email: str | None
    address: dict[str, str]
    state_code: str | None
    precision_m: float | None
    ids: dict[str, str]
    amenities: dict[str, str]
    num_sites: int | None
    sites_by_type: dict[str, int]
    max_rv_length_ft: int | None
    reservable: bool | None
    first_come_first_served: bool | None
    price_min: float | None
    price_max: float | None
    currency: str | None
    season: list[dict[str, Any]]
    months_open: list[int] | None
    season_note: str | None
    raw: dict[str, Any]
    flags: list[Flag]
