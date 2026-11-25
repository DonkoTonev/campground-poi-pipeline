"""HTML → cleaned HTML → Markdown → plain text, one page at a time."""

from __future__ import annotations

import re
from dataclasses import dataclass

import trafilatura
from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser

DROP_TAGS = (
    "script, style, noscript, template, svg, iframe, canvas, form, nav, footer, picture, "
    "video, audio, object, embed"
)
BANNER = re.compile(r"cookie|consent|gdpr|newsletter|popup|modal|subscribe|chat-widget", re.I)
DATA_URI = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.I)
KEEP_HREF = re.compile(r"^(tel:|mailto:)|reserv|book", re.I)
MIN_MARKDOWN_SHARE = 0.25  # trafilatura kept less than this share of the text: fall back
MAX_SALVAGE_LINE = 300
FACT_SIGNAL = re.compile(
    r"\$\s?\d|\(?\b\d{3}\)?[-. /]\d{3}[-. ]\d{4}\b|@[a-z0-9-]+\.[a-z]{2,}|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d|"
    r"\b(open|closed|season|hours|check[- ]?in|check[- ]?out|memorial day|labor day)\b|"
    r"\b\d+\s+(camp)?sites\b",
    re.I,
)


@dataclass(frozen=True)
class Representations:
    raw_html: str
    cleaned_html: str
    markdown: str
    text: str


def collapse(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_html(html: str) -> str:
    """Drop everything that is never content: scripts, styles, navigation, footers, forms,
    media, cookie/newsletter banners, comments, inline base64 blobs, and every attribute
    except links that carry a phone, email or reservation target."""
    tree = LexborHTMLParser(DATA_URI.sub("", html))
    for node in tree.css(DROP_TAGS):
        node.decompose()
    for node in tree.css("[class], [id]"):
        ident = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if BANNER.search(ident):
            node.decompose()
    body = tree.body
    if body is None:
        return ""
    for node in body.traverse(include_text=False):
        href = node.attributes.get("href") if node.tag == "a" else None
        for attr in list(node.attributes):
            del node.attrs[attr]
        if href and KEEP_HREF.search(href):
            node.attrs["href"] = href
    cleaned = body.html or ""
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.S)
    return collapse(cleaned)


def to_markdown(raw_html: str, cleaned_html: str) -> str:
    """Main-content Markdown (tables and lists kept). trafilatura finds the main content;
    when it keeps too little (it can drop sidebars holding hours or amenities), convert the
    whole cleaned page instead."""
    full = collapse(markdownify(cleaned_html, heading_style="ATX", strip=["img"]))
    main = trafilatura.extract(
        raw_html,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        include_comments=False,
        favor_recall=True,
    )
    main = collapse(main or "")
    if len(main) < MIN_MARKDOWN_SHARE * len(full):
        return full
    return headline(raw_html, main) + main + salvage(full, main)


def headline(raw_html: str, main: str) -> str:
    """The page title and first H1 when main-content extraction dropped them: trafilatura
    treats a leading H1 as the page title and removes it, and that H1 is usually the
    campground's name (found by the extraction ablation: "Granite Flat Campground" vanished)."""
    tree = LexborHTMLParser(raw_html)
    lines = []
    title = tree.css_first("title")
    h1 = tree.css_first("h1")
    if h1 is not None and (text := collapse(h1.text())) and text not in main:
        lines.append(f"# {text}")
    if title is not None and (text := collapse(title.text())) and text not in main:
        lines.append(f"Page title: {text}")
    return "\n\n".join(lines) + "\n\n" if lines else ""


def salvage(full: str, main: str) -> str:
    """Lines outside the main content that carry a fact (a phone in the header, prices in a
    sidebar, hours in the footer). Appended once, compactly; repeats across a site's pages
    are removed later by boilerplate detection."""
    kept = [
        line.strip()
        for line in full.splitlines()
        if FACT_SIGNAL.search(line) and len(line) <= MAX_SALVAGE_LINE and line.strip() not in main
    ]
    unique = list(dict.fromkeys(kept))
    return (
        "\n\nOther facts on this page:\n" + "\n".join(f"- {line}" for line in unique)
        if unique
        else ""
    )


def to_text(markdown: str) -> str:
    text = re.sub(r"^#+\s*", "", markdown, flags=re.M)  # headings
    text = re.sub(r"[*_`|>]+", " ", text)  # emphasis, tables, quotes
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)  # table rules
    return collapse(text)


def represent(raw_html: str) -> Representations:
    cleaned = clean_html(raw_html)
    markdown = to_markdown(raw_html, cleaned)
    return Representations(raw_html, cleaned, markdown, to_text(markdown))


RETENTION_PATTERNS = {
    "price": re.compile(r"\$\s?\d+"),
    "phone": re.compile(r"\(?\d{3}\)?[-. /]\d{3}[-. ]\d{4}"),
    "month": re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I),
}


def fact_retention(cleaned_html: str, markdown: str) -> dict[str, tuple[int, int]]:
    """Free proxy for "did reduction lose facts": distinct prices, phone numbers and month
    mentions in the whole cleaned page vs. in the final Markdown -> {kind: (total, kept)}.
    The real accuracy check is the LLM evaluation; this one runs on every page for free."""
    full = collapse(markdownify(cleaned_html, heading_style="ATX", strip=["img"]))
    out = {}
    for kind, pattern in RETENTION_PATTERNS.items():
        total = {m.group(0).lower() for m in pattern.finditer(full)}
        kept = {m.group(0).lower() for m in pattern.finditer(markdown)}
        out[kind] = (len(total), len(total & kept))
    return out
