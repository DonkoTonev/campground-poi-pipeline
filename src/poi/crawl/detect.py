"""Classify fetched pages: blocked/captcha, parked domain, soft 404, thin (needs JS).

Heuristics are conservative and recorded as flags; a flagged page is kept, never silently
dropped, so the crawl report can show what happened to every seed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser as HTMLParser

BLOCKED = re.compile(
    r"captcha|cf-chl|attention required! \| cloudflare|access denied|request is blocked|"
    r"are you a robot|unusual traffic|bot detection|ddos protection by",
    re.IGNORECASE,
)
PARKED = re.compile(
    r"this domain (is|may be) for sale|buy this domain|domain (has )?expired|parked free|"
    r"sedoparking|parkingcrew|bodis\.com|this domain is parked|godaddy\.com/domainsearch|"
    r"hugedomains|dan\.com",
    re.IGNORECASE,
)
NOT_FOUND = re.compile(
    r"\b(404|page not found|not found|page (you|you're) looking for|no longer exists|"
    r"doesn't exist|does not exist|cannot be found|can't be found)\b",
    re.IGNORECASE,
)
SOFT_404_MAX_CHARS = 1500  # a real content page that mentions "not found" is longer


@dataclass(frozen=True)
class PageSignals:
    text_chars: int
    title: str
    flags: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, anchor text)


def visible_text(tree: HTMLParser) -> str:
    for node in tree.css("script, style, noscript, template, svg"):
        node.decompose()
    body = tree.body
    text = body.text(separator=" ") if body is not None else ""
    return re.sub(r"\s+", " ", text).strip()


def analyse(html: str, status: int, min_static_text_chars: int) -> PageSignals:
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    has_jsonld = bool(tree.css('script[type="application/ld+json"]'))
    has_next_data = tree.css_first("script#__NEXT_DATA__") is not None
    n_scripts = len(tree.css("script"))
    links = [
        (a.attributes.get("href") or "", re.sub(r"\s+", " ", a.text()).strip())
        for a in tree.css("a[href]")
    ]
    text = visible_text(tree)
    head = f"{title} {text[:3000]}"

    flags: list[str] = []
    if has_jsonld:
        flags.append("has_jsonld")
    if has_next_data:
        flags.append("has_next_data")
    if (status in (403, 429, 503) and BLOCKED.search(html[:20000])) or BLOCKED.search(title):
        flags.append("blocked")
    elif PARKED.search(head):
        flags.append("parked")
    elif status == 200 and len(text) < SOFT_404_MAX_CHARS and NOT_FOUND.search(head):
        flags.append("soft_404")
    elif (
        status == 200
        and len(text) < min_static_text_chars
        and not (has_jsonld or has_next_data)
        and n_scripts >= 3
    ):
        flags.append("needs_render")
    return PageSignals(len(text), title, flags, links)
