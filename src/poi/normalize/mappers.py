"""Per-source mapping: a `source_records` row's payload → canonical `Draft` (no cleaning)."""

from __future__ import annotations

import re
from typing import Any

from poi.normalize.model import Category, Draft, Flag, OperatorType, Severity, Tri

RECGOV_ID = re.compile(r"recreation\.gov/camping/campgrounds/(\d+)")
PO_BOX = re.compile(r"\bp\.?\s*o\.?\s*box\b", re.IGNORECASE)
OSM_ALT_NAME_TAGS = ("alt_name", "old_name", "name_old", "was:name", "official_name", "short_name")


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip().replace("$", ""))
    except (TypeError, ValueError):
        return None


def _recgov_ids(*urls: str | None) -> dict[str, str]:
    for url in urls:
        if url and (m := RECGOV_ID.search(url)):
            return {"recgov_facility_id": m.group(1)}
    return {}


# --- OSM --------------------------------------------------------------------------------

_OSM_YES_NO = {"yes": Tri.YES, "no": Tri.NO, "customers": Tri.YES, "seasonal": Tri.SEASONAL}
_OSM_AMENITY_TAGS = {
    "drinking_water": "drinking_water",
    "shower": "showers",
    "power_supply": "electric_hookups",
    "sanitary_dump_station": "dump_station",
    "picnic_table": "picnic_tables",
    "dog": "pets_allowed",
    "laundry": "laundry",
}


def _osm_amenities(tags: dict[str, str]) -> dict[str, Tri]:
    out: dict[str, Tri] = {}
    for tag, amenity in _OSM_AMENITY_TAGS.items():
        value = tags.get(tag, "").lower()
        if value in ("leashed", "hot", "cold"):
            out[amenity] = Tri.YES
        elif value in _OSM_YES_NO:
            out[amenity] = _OSM_YES_NO[value]
    disposal = tags.get("toilets:disposal", "")
    if tags.get("toilets") == "no":
        out["flush_toilets"] = out["vault_toilets"] = Tri.NO
    elif disposal == "flush":
        out["flush_toilets"] = Tri.YES
    elif disposal in ("pitlatrine", "chemical", "bucket"):
        out["vault_toilets"] = Tri.YES
    internet = tags.get("internet_access", "")
    if internet in ("wlan", "yes"):
        out["wifi"] = Tri.YES
    elif internet == "no":
        out["wifi"] = Tri.NO
    wheelchair = tags.get("wheelchair", "")
    if wheelchair in ("yes", "no"):
        out["ada_accessible"] = Tri(wheelchair)
    elif wheelchair == "limited":
        out["ada_accessible"] = Tri.PARTIAL
    return out


def map_osm(payload: dict[str, Any], lat: float | None, lon: float | None) -> Draft:
    tags: dict[str, str] = payload.get("tags", {})
    alt = [n.strip() for t in OSM_ALT_NAME_TAGS for n in tags.get(t, "").split(";") if n.strip()]
    website = tags.get("website") or tags.get("contact:website") or tags.get("url")
    reservation = tags.get("reservation:website") or tags.get("reservation:url")
    ids = {"osm_id": f"{payload['osm_type']}/{payload['osm_id']}"}
    if tags.get("wikidata"):
        ids["wikidata"] = tags["wikidata"]
    ids |= _recgov_ids(website, reservation)
    reservation_tag = tags.get("reservation")
    maxlength = _float(tags.get("maxlength"))
    sites = {
        kind: n
        for kind, tag in (("tent", "capacity:tents"), ("rv", "capacity:caravans"))
        if (n := _int(tags.get(tag))) is not None
    }
    return Draft(
        name=tags.get("name"),
        alt_names=alt,
        raw_lat=lat,
        raw_lon=lon,
        phone=tags.get("phone") or tags.get("contact:phone"),
        website=website,
        reservation_url=reservation,
        email=tags.get("email") or tags.get("contact:email"),
        address={
            k.removeprefix("addr:"): v
            for k, v in tags.items()
            if k in ("addr:housenumber", "addr:street", "addr:city", "addr:state", "addr:postcode")
        },
        claimed_state=tags.get("addr:state"),
        operator_name=tags.get("operator"),
        ids=ids,
        amenities=_osm_amenities(tags),
        num_sites=_int(tags.get("capacity")),
        sites_by_type=sites,
        max_rv_length=(maxlength, "m") if maxlength else None,
        reservable={"yes": True, "required": True, "recommended": True, "no": False}.get(
            reservation_tag or ""
        ),
        season_raw=tags.get("opening_hours"),
        season_note=tags.get("seasonal") if tags.get("seasonal") not in (None, "no") else None,
        tags=tags,
    )


# --- RIDB -------------------------------------------------------------------------------


def map_ridb(payload: dict[str, Any]) -> Draft:
    facility_id = str(payload["FacilityID"])
    addresses = payload.get("FACILITYADDRESS") or []
    addr = addresses[0] if addresses else {}
    street = addr.get("FacilityStreetAddress1") or ""
    flags: list[Flag] = []
    if PO_BOX.search(street):
        flags.append(Flag("address", "po_box_address", Severity.INFO))
    if payload.get("_region_by") == "recarea":
        flags.append(Flag("geometry", "region_by_recarea", Severity.WARNING))
    orgs = payload.get("ORGANIZATION") or []
    reservable = payload.get("Reservable")
    return Draft(
        name=payload.get("FacilityName"),
        raw_lat=payload.get("FacilityLatitude"),
        raw_lon=payload.get("FacilityLongitude"),
        phone=payload.get("FacilityPhone"),
        email=payload.get("FacilityEmail"),
        reservation_url=(
            f"https://www.recreation.gov/camping/campgrounds/{facility_id}"
            if reservable
            else payload.get("FacilityReservationURL") or None
        ),
        address={
            k: v
            for k, v in (
                ("street", street),
                ("city", addr.get("City") or ""),
                ("state", addr.get("AddressStateCode") or ""),
                ("postcode", addr.get("PostalCode") or ""),
            )
            if v
        },
        claimed_state=addr.get("AddressStateCode") or None,
        operator_name=orgs[0].get("OrgName") if orgs else None,
        operator_type_hint=OperatorType.FEDERAL,
        trusted_campground=True,
        ids={"ridb_facility_id": facility_id, "recgov_facility_id": facility_id},
        # RIDB's FacilityAdaAccess is "N" or blank for every record in our data, so "N" is
        # not evidence of inaccessibility. Only "Y" is used.
        amenities={"ada_accessible": Tri.YES} if payload.get("FacilityAdaAccess") == "Y" else {},
        reservable=reservable if isinstance(reservable, bool) else None,
        flags=flags,
    )


# --- NPS --------------------------------------------------------------------------------


def _nps_tri(value: Any) -> Tri:
    """NPS amenity strings: "Yes - year round", "Yes - seasonal", "No", "None", ""."""
    text = (value[0] if isinstance(value, list) and value else value or "").lower()
    if not text:
        return Tri.UNKNOWN
    if text.startswith(("no", "none")):
        return Tri.NO
    if "seasonal" in text:
        return Tri.SEASONAL
    return Tri.YES


def _nps_amenities(amen: dict[str, Any], campsites: dict[str, Any]) -> dict[str, Tri]:
    out: dict[str, Tri] = {}
    for key, amenity in (
        ("potableWater", "drinking_water"),
        ("showers", "showers"),
        ("dumpStation", "dump_station"),
        ("laundry", "laundry"),
        ("campStore", "camp_store"),
        ("internetConnectivity", "wifi"),
    ):
        if (tri := _nps_tri(amen.get(key))) is not Tri.UNKNOWN:
            out[amenity] = tri
    toilets = [t.lower() for t in amen.get("toilets") or []]
    for kind, amenity in (("flush", "flush_toilets"), ("vault", "vault_toilets")):
        match = [t for t in toilets if t.startswith(kind)]
        if match:
            out[amenity] = Tri.SEASONAL if all("seasonal" in t for t in match) else Tri.YES
        elif toilets and all(t.startswith(("none", "no")) for t in toilets):
            out[amenity] = Tri.NO
    total, electric = _int(campsites.get("totalSites")), _int(campsites.get("electricalHookups"))
    if total and electric is not None:
        # An explicit per-campground count from the operator, not a default.
        out["electric_hookups"] = (
            Tri.NO if electric == 0 else Tri.YES if electric >= total else Tri.PARTIAL
        )
    return out


def map_nps(payload: dict[str, Any]) -> Draft:
    contacts = payload.get("contacts") or {}
    phones = [p for p in contacts.get("phoneNumbers") or [] if p.get("type", "Voice") == "Voice"]
    emails = contacts.get("emailAddresses") or []
    addresses = payload.get("addresses") or []
    physical = next((a for a in addresses if a.get("type") == "Physical"), None)
    campsites = payload.get("campsites") or {}
    access = payload.get("accessibility") or {}
    hours = payload.get("operatingHours") or []
    fees = [c for f in payload.get("fees") or [] if (c := _float(f.get("cost"))) is not None]
    reservable_n = _int(payload.get("numberOfSitesReservable"))
    fcfs_n = _int(payload.get("numberOfSitesFirstComeFirstServe"))
    total = _int(campsites.get("totalSites"))
    rv_len = _float(access.get("rvMaxLength"))
    hint = None
    if total:
        for key, cat in (
            ("horse", Category.HORSE),
            ("group", Category.GROUP),
            ("walkBoatTo", Category.TENT_WALK_IN),
        ):
            if _int(campsites.get(key)) == total:
                hint = cat
    phone = phones[0] if phones else None
    return Draft(
        name=payload.get("name"),
        raw_lat=payload.get("latitude"),
        raw_lon=payload.get("longitude"),
        phone=(
            f"{phone['phoneNumber']} x{phone['extension']}"
            if phone and phone.get("extension")
            else phone["phoneNumber"]
            if phone
            else None
        ),
        email=emails[0].get("emailAddress") if emails else None,
        website=payload.get("url") or None,
        reservation_url=payload.get("reservationUrl") or None,
        address={
            k: v
            for k, v in (
                ("street", physical.get("line1", "") if physical else ""),
                ("city", physical.get("city", "") if physical else ""),
                ("state", physical.get("stateCode", "") if physical else ""),
                ("postcode", physical.get("postalCode", "") if physical else ""),
            )
            if v
        },
        claimed_state=(physical or {}).get("stateCode") or None,
        operator_name="National Park Service",
        operator_type_hint=OperatorType.FEDERAL,
        category_hint=hint,
        trusted_campground=True,
        ids={"nps_id": str(payload["id"]), "nps_park": str(payload.get("parkCode", ""))}
        | _recgov_ids(payload.get("reservationUrl")),
        amenities=_nps_amenities(payload.get("amenities") or {}, campsites),
        num_sites=total or None,
        sites_by_type={
            k: n
            for k in ("tentOnly", "rvOnly", "group", "horse", "walkBoatTo", "other")
            if (n := _int(campsites.get(k)))
        },
        max_rv_length=(rv_len, "ft") if rv_len else None,
        reservable=None if reservable_n is None else reservable_n > 0,
        first_come_first_served=None if fcfs_n is None else fcfs_n > 0,
        price_min=min(fees) if fees else None,
        price_max=max(fees) if fees else None,
        currency="USD" if fees else None,
        season_raw=hours,
        season_note=next((h.get("description") for h in hours if h.get("description")), None),
    )
