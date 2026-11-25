"""Content reduction: cleaning, Markdown with fact salvage, boilerplate, caps, deterministic."""

from poi.reduce import bundle, deterministic, ridb
from poi.reduce.html import clean_html, fact_retention, represent, to_text
from poi.reduce.tokens import estimate

FILLER = "<p>" + "Our campground sits among tall pines next to the river. " * 12 + "</p>"
PAGE = f"""<html><head><title>Pine Test</title><style>.x{{color:red}}</style>
<script>var tracking = 1;</script></head><body>
<nav><a href="/">Home</a> <a href="/rates">Rates</a></nav>
<div class="cookie-banner">We use cookies. Accept?</div>
<header>Call us: <a href="tel:+17605550142">(760) 555-0142</a></header>
<main><h1>Pine Test Campground</h1>{FILLER}
<h2>Rates</h2><table><tr><th>Site</th><th>Nightly</th></tr>
<tr><td>Full hookup</td><td>$55</td></tr><tr><td>Tent</td><td>$30</td></tr></table>
<p>Open May 15 through October 1. Showers and laundry available.</p>
<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk">
<a href="https://www.campspot.com/book/pine" class="btn">Reserve now</a></main>
<form><input name="email"></form>
<footer>Email <a href="mailto:info@pinetest.example.com">us</a>, or
<a href="mailto:jane.doe@pinetest.example.com">Jane</a>. 123 Pine Rd, Lee Vining, CA</footer>
<!-- comment --><svg><path d="M0 0"/></svg></body></html>"""


def test_clean_html_keeps_content_drops_everything_else() -> None:
    cleaned = clean_html(PAGE)
    for gone in (
        "tracking",
        "color:red",
        "cookies",
        "<nav",
        "<form",
        "<svg",
        "base64",
        "comment",
        "<footer",
        'class="',
    ):
        assert gone not in cleaned, gone
    assert 'href="tel:+17605550142"' in cleaned
    assert 'href="https://www.campspot.com/book/pine"' in cleaned  # reservation link kept
    assert "$55" in cleaned


def test_markdown_keeps_tables_and_salvages_facts_outside_main_content() -> None:
    reps = represent(PAGE)
    assert "$55" in reps.markdown and "$30" in reps.markdown
    assert "May 15" in reps.markdown
    assert "555-0142" in reps.markdown  # header phone, outside the main content
    assert estimate(reps.raw_html) > estimate(reps.cleaned_html) > estimate(reps.markdown)
    assert "#" not in to_text("# Rates\n\n| a | b |")


def test_fact_retention_counts_what_survived() -> None:
    reps = represent(PAGE)
    retention = fact_retention(reps.cleaned_html, reps.markdown)
    assert retention["price"] == (2, 2)
    total, kept = retention["phone"]
    assert total >= 1 and kept == total


def _pages(*bodies: str) -> list[bundle.PageInput]:
    return [
        bundle.PageInput(f"P{i}", f"https://x.example.com/{i}", b)
        for i, b in enumerate(bodies, start=1)
    ]


def test_boilerplate_is_removed_and_footer_facts_kept_once_site_wide() -> None:
    footer = "Home | Rates | Contact\nCall (760) 555-0142 for reservations"
    pages = _pages(
        f"Rates: $40 per night\n{footer}", f"Showers: yes\n{footer}", f"Pets on leash\n{footer}"
    )
    b = bundle.build("x.example.com", pages, 12_000)
    assert b.text.count("555-0142") == 1 and "[SITE]" in b.text
    assert "Home | Rates | Contact" not in b.text
    assert b.boilerplate_lines_removed == 6
    assert "[P1]" in b.text and "[P3]" in b.text and not b.truncated


def test_cap_cuts_by_relevance_only_when_needed() -> None:
    fluff = "\n".join(f"Our story chapter {i}: a family tradition since 1952." for i in range(400))
    facts = "## Amenities\nShowers: seasonal\nRV sites up to 40 ft\nOpen May 15 to Oct 1"
    b = bundle.build("x.example.com", _pages(fluff + "\n" + facts), 1_000)
    assert b.truncated and b.tokens <= 1_000
    for line in ("## Amenities", "Showers: seasonal", "Open May 15 to Oct 1"):
        assert line in b.text
    small = bundle.build("x.example.com", _pages(facts), 1_000)
    assert not small.truncated and facts in small.text


def test_deterministic_facts_from_jsonld_links_and_address() -> None:
    jsonld = """<script type="application/ld+json">{"@context": "https://schema.org",
      "@graph": [{"@type": "Event", "name": "Bluegrass night", "telephone": "+17605550111"},
                 {"@type": ["Campground", "LodgingBusiness"], "name": "Pine Test Campground",
                  "telephone": "+1-760-555-0142",
                  "address": {"@type": "PostalAddress", "streetAddress": "123 Pine Rd",
                              "addressLocality": "Lee Vining", "addressRegion": "CA"},
                  "geo": {"@type": "GeoCoordinates", "latitude": 37.95, "longitude": -119.1}}]}
      </script>"""
    html = PAGE.replace("</head>", jsonld + "</head>").replace(
        "<footer>", "<footer><address>123 Pine Rd, Lee Vining, CA 93541</address>"
    )
    facts = deterministic.extract(html, "https://pinetest.example.com", "P1")
    by = {(f.field, f.method): f.value for f in facts}
    assert by[("Campground.telephone", "jsonld")] == "+1-760-555-0142"
    assert by[("Campground.geo", "jsonld")] == {"lat": 37.95, "lon": -119.1}
    assert not any("Event" in f.field or f.value == "Bluegrass night" for f in facts)
    assert by[("phone", "tel_link")] == "+17605550142"
    emails = [f.value for f in facts if f.method == "mailto_link"]
    assert emails == ["info@pinetest.example.com"]  # personal address dropped
    assert "Lee Vining" in by[("address_text", "address_tag")]


def test_ridb_description_document() -> None:
    payload = {
        "FacilityID": "232447",
        "FacilityName": "Upper Pines Campground",
        "FacilityDescription": "<h2>Overview</h2><p>Open all year. <b>Flush toilets</b>.</p>",
        "FacilityUseFeeDescription": "",
        "StayLimit": "14 days",
    }
    doc = ridb.document(payload)
    assert doc is not None
    assert doc.startswith("# Upper Pines Campground (Recreation.gov facility 232447)")
    assert "[R1]" in doc and "**Flush toilets**" in doc and "14 days" in doc
    assert "## Fees" not in doc
    assert ridb.document({"FacilityID": "1", "FacilityDescription": " "}) is None


def test_markdown_keeps_a_leading_h1_and_the_title() -> None:
    from poi.reduce.html import represent

    html = (
        "<html><head><title>Granite Flat | Example Forest</title></head><body>"
        "<h1>Granite Flat Campground</h1><p>A primitive campground with 18 sites. "
        "There are no hookups. Showers are not available.</p></body></html>"
    )
    md = represent(html).markdown
    assert md.count("Granite Flat Campground") == 1  # kept or restored, never doubled
    assert "Page title: Granite Flat | Example Forest" in md
