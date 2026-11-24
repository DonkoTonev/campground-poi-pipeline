"""Category, campground/non-campground detection and operator type.

Rules are ordered and conservative. A name alone never removes a record from a trusted
federal campground listing (RIDB/NPS): "Camp 4" is a famous campground. Non-campground
decisions are flagged with the evidence, so a reviewer can override them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from poi.normalize.model import Category, Flag, OperatorType, Severity

CAMP_WORDS = re.compile(r"\b(campground|campsites?|rv|camping|backpackers?)\b")
HORSE = re.compile(r"\b(horse|equestrian|stock)\b")
GROUP = re.compile(r"\bgroup\b")
DISPERSED = re.compile(r"\bdispersed\b")
RV_PARK = re.compile(r"\brv (park|resort|sites?)\b")
WALK_IN = re.compile(r"\b(walk in|backpackers?|tent only|hike in|boat in)\b")
LODGING = re.compile(r"\b(lodge|cabins|village|resort|motel|inn)\b")
ANY_CAMP = re.compile(r"\bcamp(ground|site)?s?\b")
DAY_USE = re.compile(r"\b(day use|picnic area|trailhead|visitor center|boat launch)\b")
AGGREGATE = re.compile(r"ranger district campgrounds|corridor\b|^campgrounds\b")
ORGANIZED_NAME = re.compile(r"\b(family|senior|youth|summer|scout|bible|church|christian) camp\b")
ORGANIZED_OPERATOR = re.compile(
    r"universit|college|alumni|church|adventist|bible|christian|scout|ymca|ywca|ministr"
)

OPERATOR_PATTERNS: tuple[tuple[OperatorType, re.Pattern[str]], ...] = (
    (
        OperatorType.FEDERAL,
        re.compile(
            r"forest service|\busfs\b|\bnfs\b|\busda\b|national forest|national parks? service"
            r"|\bnps\b|national park|bureau of land|\bblm\b|army corps|fish and wildlife"
            r"|bureau of reclamation"
        ),
    ),
    (OperatorType.STATE, re.compile(r"\bstate parks?\b|calif\w* state|state of |university of")),
    (OperatorType.COUNTY, re.compile(r"\bcounty\b")),
    (OperatorType.MUNICIPAL, re.compile(r"\bcity of\b|\btown of\b|department of parks|district")),
    (OperatorType.TRIBAL, re.compile(r"\btrib(e|al)\b|rancheria|band of")),
)


def operator_type(operator: str | None, hint: OperatorType | None = None) -> OperatorType:
    if operator:
        text = operator.lower()
        for kind, pattern in OPERATOR_PATTERNS:
            if pattern.search(text):
                return kind
    if hint is not None:
        return hint
    return OperatorType.PRIVATE if operator else OperatorType.UNKNOWN


def _yes(value: str | None) -> bool:
    return bool(value) and value not in ("no", "0")


@dataclass(frozen=True)
class Classification:
    category: Category
    is_campground: bool
    exclusion_reason: str | None
    flags: list[Flag]


def classify(
    raw_name: str | None,
    match_name: str | None,
    tags: dict[str, str],
    operator: str | None,
    hint: Category | None,
    trusted_campground: bool,
    alt_names: tuple[str, ...] = (),
) -> Classification:
    """`match_name` and `alt_names` are matching keys (lower case, abbreviations expanded)."""
    name = match_name or ""
    op = (operator or "").lower()
    flags: list[Flag] = []

    exclusion: str | None = None
    if is_aggregate(raw_name):
        exclusion = "aggregate_facility"  # one listing standing for many campgrounds
    elif DAY_USE.search(name) and not ANY_CAMP.search(name):
        exclusion = "day_use_or_trailhead"
    elif _organized_camp(name, op, tags):
        exclusion = "organized_camp"
    elif (
        LODGING.search(name)
        and not CAMP_WORDS.search(name)
        and _yes(tags.get("cabins"))
        and tags.get("tents") != "yes"
        and tags.get("caravans") != "yes"
    ):
        exclusion = "lodging_only"

    # A trusted federal listing outweighs weak name evidence, but not an explicit
    # "Day Use Area" name: RIDB lists some day-use areas with type Campground.
    if trusted_campground and exclusion in ("organized_camp", "lodging_only"):
        flags.append(
            Flag("category", "possible_non_campground", Severity.INFO, {"reason": exclusion})
        )
        exclusion = None
    elif (
        exclusion is None
        and not trusted_campground
        and name.startswith("camp ")
        and not any(CAMP_WORDS.search(n) for n in alt_names)
        and not CAMP_WORDS.search(name)
    ):
        # "Camp <Name>" without campground words: often a youth/organized camp. Review, keep.
        flags.append(
            Flag("category", "possible_non_campground", Severity.INFO, {"reason": "camp_prefix"})
        )
    if exclusion:
        flags.append(Flag("category", "non_campground", Severity.WARNING, {"reason": exclusion}))

    category = _category(name, tags, hint)
    return Classification(category, exclusion is None, exclusion, flags)


def _organized_camp(name: str, operator: str, tags: dict[str, str]) -> bool:
    """Youth/family/church/university camps: tagged, named, or run by such an operator.
    "Camp <Name>" run by a city is a municipal family camp, not a public campground."""
    return (
        tags.get("leisure") == "summer_camp"
        or bool(ORGANIZED_NAME.search(name))
        or bool(ORGANIZED_OPERATOR.search(operator))
        or (name.startswith("camp ") and not CAMP_WORDS.search(name) and "city of" in operator)
    )


def _category(name: str, tags: dict[str, str], hint: Category | None) -> Category:
    if tags.get("backcountry") == "yes":
        return Category.BACKCOUNTRY
    if HORSE.search(name):
        return Category.HORSE
    if tags.get("group_only") == "yes" or GROUP.search(name):
        return Category.GROUP
    if DISPERSED.search(name) or tags.get("impromptu") == "yes":
        return Category.DISPERSED
    if hint is not None:
        return hint
    if tags.get("tourism") == "caravan_site" or RV_PARK.search(name):
        return Category.RV_PARK
    if WALK_IN.search(name):
        return Category.TENT_WALK_IN
    if _yes(tags.get("cabins")) and tags.get("tents") != "yes" and tags.get("caravans") != "yes":
        return Category.CABINS_GLAMPING
    return Category.DEVELOPED


def is_aggregate(raw_name: str | None) -> bool:
    """RIDB lists some pseudo-facilities that stand for many campgrounds."""
    if not raw_name:
        return False
    return bool(AGGREGATE.search(raw_name.lower()))
