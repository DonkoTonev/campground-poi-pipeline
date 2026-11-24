"""Grounding: is every extracted value supported by the input text? Deterministic and free.

- Directly checkable values (phone, email, prices, site count, RV length, coordinates) must
  appear in the document. If not, they are dropped: that is a hallucination.
- Evidence quotes must appear in the document (after normalizing case, whitespace, quotes
  and dashes; a small fuzzy tolerance covers markdown artefacts). An ungrounded quote is
  dropped, and fields that needed it (season, status, amenities, reservability) are marked
  unsupported.
- Confidence is computed from these checks and agreement with deterministic facts; the
  model is never asked how sure it is.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from poi.normalize.contact import normalize_phone

FUZZY_QUOTE_MIN = 92
EVIDENCE_FIELDS = ("season", "status", "reservable", "first_come_first_served")
AMENITY_PREFIX = "amenities."


# Curly quotes and dashes -> ASCII (by code point, so the source stays ASCII).
_PUNCT = str.maketrans(
    {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-"}
)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().translate(_PUNCT)
    text = re.sub(r"[*_`#|>]+", " ", text)  # markdown markup
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class GroundingReport:
    checked: int = 0
    dropped: list[str] = field(default_factory=list)  # hallucinated values removed
    unsupported: list[str] = field(default_factory=list)  # kept, but no grounded evidence
    bad_quotes: int = 0
    good_quotes: int = 0
    confidence: dict[str, float] = field(default_factory=dict)

    @property
    def hallucination_rate(self) -> float:
        return len(self.dropped) / self.checked if self.checked else 0.0


class Grounder:
    def __init__(self, document: str) -> None:
        self.text = normalize_text(document)
        self.digits = re.sub(r"\D", "", document)

    def quote_ok(self, quote: str) -> bool:
        q = normalize_text(quote)
        if not q:
            return False
        if q in self.text:
            return True
        return len(q) >= 12 and fuzz.partial_ratio(q, self.text) >= FUZZY_QUOTE_MIN

    def number_ok(self, value: float, money: bool = False) -> bool:
        n = f"{value:g}"
        whole = f"{int(value)}" if float(value).is_integer() else n
        pattern = rf"\$\s?{re.escape(whole)}(\.\d\d)?\b" if money else rf"\b{re.escape(n)}\b"
        if re.search(pattern, self.text):
            return True
        return bool(not money and re.search(rf"\b{re.escape(whole)}\b", self.text))

    def phone_ok(self, phone: str) -> bool:
        digits = re.sub(r"\D", "", phone)[-10:]
        return len(digits) == 10 and digits in self.digits

    def email_ok(self, email: str) -> bool:
        return email.lower() in self.text


def ground(
    campground: dict[str, Any], document: str, deterministic: list[dict[str, Any]] | None = None
) -> tuple[dict[str, Any], GroundingReport]:
    """Return the campground with unsupported values removed, and what was checked."""
    g = Grounder(document)
    report = GroundingReport()
    out = dict(campground)

    def check(fld: str, ok: bool) -> None:
        report.checked += 1
        if ok:
            report.confidence[fld] = 0.9
        else:
            report.dropped.append(fld)

    if out.get("phone"):
        ok = g.phone_ok(out["phone"])
        check("phone", ok)
        if not ok:
            out.pop("phone")
    if out.get("email"):
        ok = g.email_ok(out["email"])
        check("email", ok)
        if not ok:
            out.pop("email")
    for fld in ("price_min", "price_max"):
        if out.get(fld) is not None:
            ok = g.number_ok(float(out[fld]), money=True)
            check(fld, ok)
            if not ok:
                out.pop(fld)
    if out.get("num_sites") is not None:
        ok = g.number_ok(float(out["num_sites"]))
        check("num_sites", ok)
        if not ok:
            out.pop("num_sites")
    if out.get("max_rv_length"):
        ok = g.number_ok(float(out["max_rv_length"]["value"]))
        check("max_rv_length", ok)
        if not ok:
            out.pop("max_rv_length")
    for fld in ("latitude", "longitude"):
        if out.get(fld) is not None:
            ok = f"{abs(float(out[fld])):.3f}"[:-1] in g.text  # 2 decimals written
            check(fld, ok)
            if not ok:
                out.pop(fld)

    good_evidence: set[str] = set()
    kept_evidence = []
    for ev in out.get("evidence", []):
        if g.quote_ok(ev.get("quote", "")):
            report.good_quotes += 1
            good_evidence.add(ev.get("field", ""))
            kept_evidence.append(ev)
        else:
            report.bad_quotes += 1
    out["evidence"] = kept_evidence

    def supported(fld: str) -> bool:
        return fld in good_evidence or (fld == "season" and "season_text" in good_evidence)

    for fld in EVIDENCE_FIELDS:
        if out.get(fld) not in (None, []):
            report.confidence[fld] = 0.8 if supported(fld) else 0.4
            if not supported(fld):
                report.unsupported.append(fld)
    for amenity, value in (out.get("amenities") or {}).items():
        if value is not None:
            key = f"{AMENITY_PREFIX}{amenity}"
            ok = supported(amenity) or supported(key) or supported("amenities")
            report.confidence[key] = 0.8 if ok else 0.4
            if not ok:
                report.unsupported.append(key)

    for fact in deterministic or []:  # agreement with deterministic extraction
        if fact["field"] == "phone" and out.get("phone"):
            same = re.sub(r"\D", "", out["phone"])[-10:] == re.sub(r"\D", "", fact["value"])[-10:]
            report.confidence["phone"] = (
                min(1.0, report.confidence.get("phone", 0.5) + 0.1) if same else 0.3
            )
    return out, report


def phone_from_facts(facts: list[dict[str, Any]]) -> str | None:
    """The single deterministic phone of a document, if there is exactly one."""
    phones = {f["value"] for f in facts if f["field"] == "phone"}
    if len(phones) == 1:
        phone = normalize_phone(next(iter(phones)))
        return phone.e164 if phone else None
    return None
