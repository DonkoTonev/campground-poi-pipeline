"""One source record in, one canonical record (plus flags) out. Pure: no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from poi.normalize import category, contact, geo, names, season
from poi.normalize.mappers import map_nps, map_osm, map_ridb
from poi.normalize.model import AMENITIES, CanonicalRecord, Draft, Flag, Severity, Tri
from poi.settings import Region

FEET_PER_METER = 3.28084


@dataclass(frozen=True)
class SourceRow:
    id: int
    source: str
    source_id: str
    region: str
    payload: dict[str, Any]
    lat: float | None  # from the stored point (OSM: representative point)
    lon: float | None


def draft_for(row: SourceRow) -> Draft:
    if row.source == "osm":
        return map_osm(row.payload, row.lat, row.lon)
    if row.source == "ridb":
        return map_ridb(row.payload)
    if row.source == "nps":
        return map_nps(row.payload)
    raise ValueError(f"no mapper for source {row.source!r}")


def _contact(draft: Draft, flags: list[Flag]) -> dict[str, Any]:
    out: dict[str, Any] = {"phone": None, "phone_ext": None}
    if not contact.is_placeholder(draft.phone):
        phone = contact.normalize_phone(draft.phone)
        if phone is None:
            flags.append(Flag("phone", "invalid_phone", Severity.WARNING))
        else:
            out |= {"phone": phone.e164, "phone_ext": phone.extension}
            if phone.toll_free:
                flags.append(Flag("phone", "toll_free_phone", Severity.INFO))
    for fld in ("website", "reservation_url"):
        raw = getattr(draft, fld)
        url = contact.canonical_url(raw)
        if raw and url is None:
            flags.append(Flag(fld, "invalid_url", Severity.WARNING))
        if url and fld == "website" and contact.is_social(url):
            flags.append(Flag(fld, "social_media_website", Severity.INFO, {"url": url}))
            url = None
        out[fld] = url
    out["email"] = contact.normalize_email(draft.email)
    if not contact.is_placeholder(draft.email) and out["email"] is None:
        flags.append(Flag("email", "invalid_email", Severity.WARNING))
    return out


def _seasons(source: str, draft: Draft, today: date, flags: list[Flag]) -> list[season.SeasonRange]:
    if source == "osm" and isinstance(draft.season_raw, str):
        parsed = season.parse_osm_opening_hours(draft.season_raw)
        if parsed is None:
            flags.append(
                Flag("season", "unparsed_opening_hours", Severity.INFO, {"raw": draft.season_raw})
            )
        return parsed or []
    if source == "nps" and isinstance(draft.season_raw, list):
        ranges, season_flags = season.nps_season(draft.season_raw, today)
        flags.extend(season_flags)
        return ranges
    return []


def normalize(row: SourceRow, region: Region, today: date) -> CanonicalRecord:
    draft = draft_for(row)
    flags = list(draft.flags)

    name, fixes = names.display_name(draft.name)
    if name is None:
        flags.append(Flag("name", "missing_name", Severity.WARNING))
    flags += [Flag("name", fix, Severity.INFO, {"raw": draft.name}) for fix in fixes]
    alt_names = sorted({n for a in draft.alt_names if (n := names.display_name(a)[0])} - {name})
    match_name = names.match_key(name)

    claimed = None
    if draft.claimed_state and draft.claimed_state.strip():
        claimed = geo.admin1().code_for(draft.claimed_state)
        if claimed is None:
            flags.append(
                Flag(
                    "address", "invalid_state_code", Severity.WARNING, {"raw": draft.claimed_state}
                )
            )
        elif claimed != draft.claimed_state.strip():
            flags.append(
                Flag("address", "state_name_to_code", Severity.INFO, {"raw": draft.claimed_state})
            )
    geo_result = geo.check_coordinates(draft.raw_lat, draft.raw_lon, region, claimed)
    flags += geo_result.flags

    cls = category.classify(
        draft.name,
        match_name,
        draft.tags,
        draft.operator_name,
        draft.category_hint,
        draft.trusted_campground,
        alt_names=tuple(k for a in draft.alt_names if (k := names.match_key(a))),
    )
    flags += cls.flags

    rv_ft: int | None = None
    if draft.max_rv_length:
        value, unit = draft.max_rv_length
        rv_ft = round(value * FEET_PER_METER) if unit == "m" else round(value)
        if unit == "m":
            flags.append(
                Flag("max_rv_length_ft", "unit_converted", Severity.INFO, {"from": f"{value} m"})
            )

    seasons = _seasons(row.source, draft, today, flags)
    amenities = {a: draft.amenities.get(a, Tri.UNKNOWN).value for a in AMENITIES}

    return CanonicalRecord(
        source=row.source,
        source_id=row.source_id,
        source_record_id=row.id,
        region=row.region,
        name=name,
        match_name=match_name,
        alt_names=alt_names,
        category=cls.category,
        is_campground=cls.is_campground,
        exclusion_reason=cls.exclusion_reason,
        operator_type=category.operator_type(draft.operator_name, draft.operator_type_hint),
        operator_name=names.clean(draft.operator_name),
        **_contact(draft, flags),
        address=draft.address,
        state_code=geo_result.state_code,
        precision_m=geo_result.precision_m,
        ids=draft.ids,
        amenities=amenities,
        num_sites=draft.num_sites,
        sites_by_type=draft.sites_by_type,
        max_rv_length_ft=rv_ft,
        reservable=draft.reservable,
        first_come_first_served=draft.first_come_first_served,
        price_min=draft.price_min,
        price_max=draft.price_max,
        currency=draft.currency,
        season=[s.to_dict() for s in seasons],
        months_open=season.months_open(seasons, today.year) if seasons else None,
        season_note=draft.season_note,
        raw={
            "name": draft.name,
            "phone": draft.phone,
            "website": draft.website,
            "email": draft.email,
            "lat": draft.raw_lat,
            "lon": draft.raw_lon,
            "operator": draft.operator_name,
        },
        flags=flags,
    )
