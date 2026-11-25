"""The extraction contract. Lean on purpose: output tokens cost more than input tokens.

Unknown fields are omitted (defaults), never emitted as nulls or guessed. Amenities are
tri-state plus nuance and have no "no" default. No coordinates unless written on the page;
no personal names (data minimization). Evidence quotes are short and only for key fields.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PageKind = Literal[
    "campground_detail",
    "multi_campground",
    "park_overview",
    "not_campground",
    "closure_notice",
    "dead_or_parked",
]
Category = Literal[
    "developed_campground",
    "rv_park",
    "tent_walk_in",
    "group",
    "horse",
    "dispersed",
    "cabins_glamping",
    "backcountry",
    "other",
]
OperatorType = Literal["federal", "state", "county", "municipal", "tribal", "private"]
Status = Literal["open", "seasonal", "temporarily_closed", "permanently_closed"]
Tri = Literal["yes", "no", "seasonal", "partial"]
Anchor = Literal[
    "memorial_day",
    "memorial_day_weekend",
    "independence_day",
    "labor_day",
    "columbus_day",
    "thanksgiving",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # additionalProperties: false


class Address(Strict):
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class SeasonRange(Strict):
    """An open period. Each bound is a month/day, or a holiday anchor ("Memorial Day")."""

    start_month: int | None = None
    start_day: int | None = None
    start_anchor: Anchor | None = None
    end_month: int | None = None
    end_day: int | None = None
    end_anchor: Anchor | None = None
    year: int | None = Field(default=None, description="Only if the page states the year.")
    approximate: bool | None = Field(default=None, description="'weather permitting', 'mid-June'")


class SiteCount(Strict):
    type: str
    count: int


class Length(Strict):
    value: float
    unit: Literal["ft", "m"]


class Amenities(Strict):
    drinking_water: Tri | None = None
    flush_toilets: Tri | None = None
    vault_toilets: Tri | None = None
    showers: Tri | None = None
    electric_hookups: Tri | None = None
    water_hookups: Tri | None = None
    sewer_hookups: Tri | None = None
    dump_station: Tri | None = None
    laundry: Tri | None = None
    camp_store: Tri | None = None
    wifi: Tri | None = None
    fire_rings: Tri | None = None
    picnic_tables: Tri | None = None
    pets_allowed: Tri | None = None
    generators_allowed: Tri | None = None
    ada_accessible: Tri | None = None


class Evidence(Strict):
    field: str
    quote: str = Field(description="Verbatim from the document, at most 15 words.")
    page_ref: str = Field(description="The section marker, e.g. P1, SITE or R1.")


class Campground(Strict):
    name: str
    alt_names: list[str] = Field(default_factory=list)
    category: Category | None = None
    operator_type: OperatorType | None = None
    operator_name: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    reservation_url: str | None = None
    booking_platform: str | None = None
    address: Address | None = None
    latitude: float | None = Field(default=None, description="Only if written on the page.")
    longitude: float | None = Field(default=None, description="Only if written on the page.")
    status: Status | None = None
    status_note: str | None = None
    status_as_of: str | None = Field(default=None, description="YYYY-MM-DD stated on the page")
    season: list[SeasonRange] = Field(default_factory=list)
    season_text: str | None = None
    check_in: str | None = None
    check_out: str | None = None
    num_sites: int | None = None
    sites_by_type: list[SiteCount] = Field(default_factory=list)
    max_rv_length: Length | None = None
    price_min: float | None = None
    price_max: float | None = None
    currency: Literal["USD", "CAD"] | None = None
    price_year: int | None = None
    reservable: bool | None = None
    first_come_first_served: bool | None = None
    amenities: Amenities = Field(default_factory=Amenities)
    evidence: list[Evidence] = Field(default_factory=list)


class Extraction(Strict):
    page_kind: PageKind
    campgrounds: list[Campground] = Field(default_factory=list)
