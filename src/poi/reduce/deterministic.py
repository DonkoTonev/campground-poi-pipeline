"""Facts that need no LLM: schema.org JSON-LD / microdata, tel: and mailto: links, <address>.

Step 1 of the extraction ladder: whatever is found here is not asked of the model, and it is
a free cross-check for the model's answers. Personal email addresses are dropped (data
minimization): only role mailboxes such as info@ or reservations@ are kept.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import extruct
from selectolax.lexbor import LexborHTMLParser

from poi.normalize.contact import normalize_email, normalize_phone

BUSINESS_TYPES = frozenset(
    {
        "Campground",
        "LodgingBusiness",
        "LocalBusiness",
        "Resort",
        "Hotel",
        "RVPark",
        "Park",
        "TouristAttraction",
        "Place",
        "Organization",
        "Motel",
    }
)
ROLE_MAILBOX = re.compile(
    r"^(info|contact|reserv\w*|booking\w*|office|camp\w*|rv\w*|hello|stay|front\w*|desk|"
    r"guest\w*|admin|sales|mail|inquir\w*|questions?)[0-9]*@",
    re.I,
)


@dataclass(frozen=True)
class Fact:
    field: str
    value: Any
    method: str  # jsonld | microdata | tel_link | mailto_link | address_tag
    page_ref: str


def _types(item: dict[str, Any]) -> set[str]:
    t = item.get("@type") or item.get("type") or []
    return {str(x).split("/")[-1] for x in (t if isinstance(t, list) else [t])}


def _flatten(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            out.append(item)
            out += _flatten(item.get("@graph") or [])
    return out


def _structured(html: str, url: str, ref: str) -> list[Fact]:
    try:
        data = extruct.extract(
            html, base_url=url, syntaxes=["json-ld", "microdata"], uniform=True, errors="ignore"
        )
    except Exception:  # malformed markup: deterministic extraction is best effort
        return []
    facts: list[Fact] = []
    for syntax, method in (("json-ld", "jsonld"), ("microdata", "microdata")):
        for item in _flatten(data.get(syntax) or []):
            if not (_types(item) & BUSINESS_TYPES):
                continue
            item_type = sorted(_types(item) & BUSINESS_TYPES)[0]
            for key in (
                "name",
                "telephone",
                "email",
                "priceRange",
                "url",
                "checkinTime",
                "checkoutTime",
                "petsAllowed",
                "openingHours",
            ):
                if item.get(key):
                    facts.append(Fact(f"{item_type}.{key}", item[key], method, ref))
            address = item.get("address")
            if isinstance(address, dict):
                parts = {
                    k: address[k]
                    for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")
                    if address.get(k)
                }
                if parts:
                    facts.append(Fact(f"{item_type}.address", parts, method, ref))
            geo = item.get("geo")
            if isinstance(geo, dict) and geo.get("latitude") and geo.get("longitude"):
                facts.append(
                    Fact(
                        f"{item_type}.geo",
                        {"lat": geo["latitude"], "lon": geo["longitude"]},
                        method,
                        ref,
                    )
                )
            for key in ("amenityFeature", "openingHoursSpecification"):
                if item.get(key):
                    facts.append(Fact(f"{item_type}.{key}", item[key], method, ref))
    return facts


def extract(html: str, url: str, ref: str) -> list[Fact]:
    facts = _structured(html, url, ref)
    tree = LexborHTMLParser(html)
    phones: dict[str, None] = {}
    for a in tree.css('a[href^="tel:"]'):
        phone = normalize_phone((a.attributes.get("href") or "")[4:])
        if phone:
            phones[phone.e164] = None
    facts += [Fact("phone", p, "tel_link", ref) for p in phones]
    emails: dict[str, None] = {}
    for a in tree.css('a[href^="mailto:"]'):
        email = normalize_email((a.attributes.get("href") or "").split("?")[0])
        if email and ROLE_MAILBOX.match(email):
            emails[email] = None
    facts += [Fact("email", e, "mailto_link", ref) for e in emails]
    for node in tree.css("address"):
        text = re.sub(r"\s+", " ", node.text()).strip()
        if text:
            facts.append(Fact("address_text", text[:300], "address_tag", ref))
    return facts


def as_dicts(facts: list[Fact]) -> list[dict[str, Any]]:
    return [asdict(f) for f in facts]
