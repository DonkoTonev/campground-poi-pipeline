"""Site bundles: a site's best pages in one LLM request, boilerplate removed, hard-capped.

Each page is a section "## [P1] <url>" so every evidence quote can cite its page. Lines that
repeat across a site's pages (menus, footers) are dropped from the pages; the ones carrying a
fact (a phone in the footer) are kept once in a "site-wide" section at the top. When the
bundle is still over the token cap, page sections are cut by relevance (headings and lines
with campground keywords first), then truncated as a last resort, and the cut is recorded.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from poi.reduce.html import FACT_SIGNAL
from poi.reduce.tokens import estimate

RELEVANT = re.compile(
    r"camp|site|rv|tent|hook ?up|amenit|shower|toilet|water|dump|pet|dog|fire|season|open|"
    r"close|rate|fee|price|night|reserv|check[- ]?in|check[- ]?out|length|ft\b|feet",
    re.I,
)


@dataclass(frozen=True)
class PageInput:
    ref: str  # "P1", "P2", ... in crawl relevance order
    url: str
    markdown: str


@dataclass
class Bundle:
    text: str
    tokens: int
    tokens_before: int  # pages as reduced, before boilerplate removal and capping
    boilerplate_lines_removed: int
    truncated: bool
    page_refs: dict[str, str] = field(default_factory=dict)


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lstrip("-*# ").lower())


def boilerplate(pages: list[PageInput]) -> set[str]:
    """Normalized lines present on at least half of the site's pages (and at least 2)."""
    if len(pages) < 2:
        return set()
    counts = Counter(
        norm for p in pages for norm in {_norm(line) for line in p.markdown.splitlines()} if norm
    )
    threshold = max(2, math.ceil(len(pages) / 2))
    return {line for line, n in counts.items() if n >= threshold}


def _cut_to_budget(section: str, budget: int) -> tuple[str, bool]:
    """Keep the most relevant lines (in original order) within `budget` tokens."""
    if estimate(section) <= budget:
        return section, False
    lines = section.splitlines()
    ranked = sorted(
        range(len(lines)),
        key=lambda i: (
            not lines[i].startswith("#"),
            not FACT_SIGNAL.search(lines[i]),
            not RELEVANT.search(lines[i]),
            i,
        ),
    )
    keep: set[int] = set()
    used = 0
    for i in ranked:
        cost = estimate(lines[i]) + 1
        if used + cost > budget:
            continue
        keep.add(i)
        used += cost
    return "\n".join(lines[i] for i in sorted(keep)), True


def build(site_name: str, pages: list[PageInput], cap_tokens: int) -> Bundle:
    before = sum(estimate(p.markdown) for p in pages)
    shared = boilerplate(pages)
    site_wide: dict[str, str] = {}
    cleaned: list[tuple[PageInput, str]] = []
    removed = 0
    for page in pages:
        kept = []
        for line in page.markdown.splitlines():
            norm = _norm(line)
            if norm in shared:
                removed += 1
                if FACT_SIGNAL.search(line):
                    site_wide.setdefault(norm, line.strip().lstrip("- "))
                continue
            kept.append(line)
        cleaned.append((page, re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()))

    header = f"# Website of {site_name}\n"
    if site_wide:
        header += "\n## [SITE] Site-wide (repeated on every page)\n" + "\n".join(
            f"- {line}" for line in site_wide.values()
        )
    budget = cap_tokens - estimate(header)
    full = [f"## [{page.ref}] {page.url}\n\n{body}" for page, body in cleaned]
    if sum(estimate(s) for s in full) <= budget:  # fits: no cutting at all
        text = header + "\n\n" + "\n\n".join(full)
        return Bundle(text, estimate(text), before, removed, False, {p.ref: p.url for p in pages})
    per_page = budget // max(len(cleaned), 1)
    sections: list[str] = []
    truncated = False
    # Unused budget of short pages flows to later pages, in relevance order.
    for i, (page, body) in enumerate(cleaned):
        remaining_pages = len(cleaned) - i
        allowance = max(per_page, (budget - sum(estimate(s) for s in sections)) // remaining_pages)
        body, cut = _cut_to_budget(body, allowance)
        truncated |= cut
        sections.append(f"## [{page.ref}] {page.url}\n\n{body}")
    text = header + "\n\n" + "\n\n".join(sections)
    return Bundle(
        text=text,
        tokens=estimate(text),
        tokens_before=before,
        boilerplate_lines_removed=removed,
        truncated=truncated,
        page_refs={p.ref: p.url for p in pages},
    )
