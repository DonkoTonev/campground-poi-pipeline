"""Names: clean display names, smart title case for ALL-CAPS feeds, matching keys."""

from __future__ import annotations

import re
import unicodedata

ACRONYMS = frozenset({"RV", "KOA", "USFS", "BLM", "NF", "NP", "SP", "OHV", "ATV", "YMCA", "CG"})
SMALL_WORDS = frozenset({"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to"})
ROMAN = re.compile(r"^(?=[IVXLC])(X{0,3})(IX|IV|V?I{0,3})$", re.IGNORECASE)
TITLE_JUNK = re.compile(
    r"^\s*(welcome to|home of)\s+|\s*[|\-\u2013\u2014]\s*(home|homepage|official site|welcome)\s*$",
    re.IGNORECASE,
)

# Matching-only expansions. Display names keep the source's wording.
EXPANSIONS = {
    "cg": "campground",
    "cgd": "campground",
    "cmpgd": "campground",
    "cmpgrd": "campground",
    "campgrnd": "campground",
    "campgrounds": "campground",
    "mt": "mount",
    "mtn": "mountain",
    "grp": "group",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "rv's": "rv",
    "rvs": "rv",
}


def clean(name: str | None) -> str | None:
    """NFKC, whitespace collapse, page-title junk removal. Returns None for empty names."""
    if name is None:
        return None
    text = unicodedata.normalize("NFKC", name)
    text = re.sub(r"\s+", " ", text).strip()
    text = TITLE_JUNK.sub("", text).strip()
    return text or None


def is_shouting(name: str) -> bool:
    letters = [c for c in name if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _title_word(word: str, first: bool) -> str:
    core = word.strip("()\"'.,")
    upper = core.upper()
    if upper in ACRONYMS or ROMAN.match(core) or (len(core) == 1 and core.isalpha()):
        return word.replace(core, upper)
    lower = word.lower()
    if not first and core.lower() in SMALL_WORDS:
        return lower
    if re.match(r"^mc[a-z]{2,}", core.lower()):  # McGee, McCloud
        return word.replace(core, "Mc" + core[2:3].upper() + core[3:].lower())
    # Capitalise after apostrophes in O'Neil style names, but not possessives ("Annett's").
    titled = "-".join(part[:1].upper() + part[1:] for part in lower.split("-"))
    return re.sub(r"\bO'([a-z])", lambda m: "O'" + m.group(1).upper(), titled)


def smart_title(name: str) -> str:
    """Title-case an ALL-CAPS name, keeping acronyms, roman numerals, loop letters, Mc-names."""
    words = name.split(" ")
    return " ".join(_title_word(w, i == 0) for i, w in enumerate(words))


def display_name(raw: str | None) -> tuple[str | None, list[str]]:
    """Return (display name, applied fixes). Fixes become info-level flags."""
    fixes: list[str] = []
    cleaned = clean(raw)
    if cleaned is None:
        return None, fixes
    if raw is not None and cleaned != re.sub(r"\s+", " ", raw).strip():
        fixes.append("title_junk_or_unicode_cleaned")
    if is_shouting(cleaned):
        cleaned = smart_title(cleaned)
        fixes.append("all_caps_recased")
    return cleaned, fixes


def match_key(name: str | None) -> str | None:
    """Lower-case, accent-free, punctuation-free form with abbreviations expanded."""
    cleaned = clean(name)
    if cleaned is None:
        return None
    text = unicodedata.normalize("NFKD", cleaned)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("'", "").replace("\u2019", "")
    tokens = re.findall(r"[a-z0-9]+", text)
    return " ".join(EXPANSIONS.get(t, t) for t in tokens) or None
