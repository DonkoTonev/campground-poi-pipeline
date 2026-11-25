# Design notes

Deeper rationale behind the pipeline. Sections are added as milestones land.

## Ingest: fetch once, process many times

Every external response is gzipped into a raw store (local filesystem or S3, same interface)
and registered in `raw_objects` with a `request_key` that hashes the logical request (source,
method, URL, parameters; API keys are sent as headers and never stored). A later run with the
same request key reads from the store unless `--refresh` is given, so parsers, normalizers and
matchers can be changed and re-run with no network traffic.

`source_records` is upserted on `(source, source_id)`. A content hash separates *changed* from
*unchanged* records, and records that a complete run did not see get `missed_runs + 1`: they
are never deleted, which later feeds a `possibly_closed` review flag instead of silently
dropping a campground because one API call misbehaved.

## Normalization: one canonical model, nothing fixed silently

Per-source mappers only extract values into a `Draft`; shared normalizers clean and validate
them. Every rule violation becomes a `quality_flags` row (stage, subject, field, rule,
severity, details) and the raw values stay in `poi_records.raw`, so any normalized value can
be traced back. Re-running `poi normalize` replaces the stage's flags instead of appending.

Rules worth calling out:

- **Unknown is not "no".** Amenities are tri-state plus `seasonal` / `partial`; a missing tag
  is `unknown`. RIDB's `FacilityAdaAccess` is "N" or blank on every record we have, so "N" is
  treated as no evidence. NPS's explicit per-campground electrical-site count of 0 is evidence.
- **Trust beats name heuristics, within limits.** A record from a federal campground listing
  (RIDB, NPS) is never excluded because of its name ("Camp 4" is a famous campground), except
  for an explicit "Day Use Area" or an aggregate pseudo-facility.
- **Coordinates are diagnosed, not repaired.** Swapped or sign-flipped pairs get a flag with
  the suggested fix; the record keeps no usable point until the fix is confirmed.
- **Display names vs matching keys.** ALL-CAPS feeds are re-cased for display (keeping RV, KOA,
  Mc-names, roman numerals and loop letters); abbreviations (CG, Mt, N/S/E/W) are expanded only
  in the matching key.
- **NPS seasons are recurring closures.** NPS usually lists only the upcoming seasonal closure,
  so the open season is the gap until the closure recurs (closed Oct 19 to Apr 21 means open
  Apr 22 to Oct 18). A first version computed the complement within one calendar year and
  produced "January to August" for a campground open mid-July to mid-August.

## Entity resolution and the 200 m question

"What if several campgrounds are within 200 m of each other?" In Yosemite Valley, Upper,
Lower and North Pines sit within about 300 m; family, group and horse camps share a lake and a
base name; an OSM polygon, an NPS point and a RIDB point for the *same* campground can be
4 km apart when one source's coordinates are wrong. So distance generates candidates and is
one weighted feature. It is never a merge rule.

**Blocking** (PostGIS, three index-backed passes, unioned): `ST_DWithin` on the
polygon-aware geography (2 km, wider for low-precision points), `pg_trgm` name similarity for
records whose coordinates can't be trusted, and exact joins on shared identifiers
(recreation.gov facility id, NPS id, wikidata).

**Scoring** is Fellegi-Sunter-style log-odds with hand-set weights in
`config/matching.yaml`: prior, distance bins (0 inside a polygon), core-name similarity (generic
words removed, former names considered), discriminator tokens, shared ids, phone and website
equality weighted by inverse frequency (a ranger-district phone shared by 20 campgrounds
counts little), and category compatibility. Every pair keeps its per-feature contributions, so
each decision is explainable.

**Discriminators** are what answer the 200 m question. "Upper" vs "Lower", "Old" vs "New",
loop letters, and hard types (group, horse, overflow, walk-in, backpacker) on one side only are
strong non-match evidence even at 25 m. "RV" and "tent" are soft (a resort and its RV park can
be one place). A shared authoritative id outweighs a discriminator: a renamed campground can
carry a different one.

**Thresholds favour precision.** A false merge deletes a real campground from the app; a false
split shows a duplicate pin. Auto-merge needs a score of 4 or more; 0 to 4 goes to review; a
name match that only works through a former name always goes to review (possible rename).

**Clustering** adds accepted edges strongest first (union-find) and refuses an edge that would
put a cannot-link pair in one cluster: two different RIDB or NPS ids, conflicting
discriminators without a shared id, a pair scored as a strong non-match, or a cluster wider
than 3 km. Refused edges become review items, so a chain A~B~C with A and C incompatible
splits on its weaker edge. Edges backed by a shared id skip the diameter guard: in real data
RIDB places Crane Flat 4.2 km and Wawona 3.2 km from where NPS and OSM put them, and those
records are kept out of later diameter checks.

**Hierarchy.** A group or horse camp, a loop, or an unnamed site inside another campground's
polygon becomes its child (`record_relations`), not a duplicate.

**Evaluation.** `poi review suggest-gold` samples about 200 pairs stratified by score bin and
oversampling hard cases (close but differently named, same name far apart, shared phone,
similar names that blocking missed). All 200 pairs were then labelled by Claude in a separate
pass that saw only the two records and their distance, never the matcher's score or decision;
each label has a one-line rationale (shared ids, distance, discriminator words) and is marked
`labeled_by: claude`. A random 50 (fixed seed) are verified by a person with `poi review label`,
which hides the drafted label, the rationale and the score bin. The steps are commands:
`poi review export-blind`, `poi review import-drafts --by claude`, `poi review draw-sample`
(seed 20261124). `poi eval er` reports
precision and recall against the drafted labels and against the human sample, the agreement
rate between the two, and every disagreement, with Wilson intervals. Drafted labels are
not ground truth; the agreement rate says how far to trust them.

**Embeddings: measured, then left off.** `poi match --embeddings` adds a local name-embedding
model (`BAAI/bge-small-en-v1.5` through fastembed, ONNX, no API calls) in two roles: a fourth
candidate source (each record's 5 most similar names at cosine ≥ 0.85 within 20 km) and a
binned log-odds feature. The feature bins were set from the similarity distribution of all
candidate pairs before looking at labels, and kept small because the feature overlaps with
name similarity. `poi eval er-embeddings` runs the three variants
([reports/er_embeddings.md](../reports/er_embeddings.md)). As a candidate source it added
21 pairs and changed no decision: blocking already generates all 52 labelled matches. As a
feature it found 2 more matches (Crags, Buckeye) and made 2 false merges ("Buckeye (Upper)"
into Buckeye), plus one unlabelled merge that looks wrong (Lake Mary Tract into Upper Lake
Mary Tract). F1 0.928 → 0.931 is noise at n = 189, and false merges cost more than missed
ones, so it stays off. The reason is visible in the vectors: "Upper Pines" vs "Lower Pines"
scores 0.82 and "Tuolumne Meadows Campground" vs "Tuolumne Meadows Horse Camp" 0.90, so the
model mostly agrees with rapidfuzz and blurs exactly the discriminators that matter here. The
caveat cuts both ways: the gold pairs were sampled from the existing candidates, so matches
only embeddings could find (renames, missing coordinates) are under-represented.

## Golden records, stable ids and manual fixes

**One entity per real campground, with an id consumers can keep.** Each `poi resolve` maps
the new clusters to the previous entities by member overlap, using record references
(`source:source_id`) rather than database ids. The entity with the largest overlap keeps its
id. When two entities merge, the loser becomes `merged` and gets an `entity_redirects` row
(chains are compressed, so every lookup is one hop). When an entity splits, the largest part
keeps the id and the rest get new ids. An entity whose records all disappear is `retired`;
nothing is hard-deleted. A test walks one entity through a split and a merge back.

**Field-level survivorship** (`config/survivorship.yaml`). Equal candidate values form a
group; group strength is the highest source trust in it plus a bonus for each additional
independent source that agrees, so two agreeing sources can outvote one more trusted source.
Trust is per field: geometry prefers a mapped OSM polygon over any agency point (entrance,
office or centroid); reservability prefers RIDB; capacity, prices and seasons prefer NPS, and
the official website (`web`) once it exists. Names are a trust-weighted vote of normalized
forms; every other spelling becomes an `alt_name`. Each `entity_fields` row keeps the winning
value, its source record, the reason it won, and every alternative with provenance.

**Conflicts are flagged, not guessed.** A disagreement where the losing side is also trusted
is a conflict. On high-impact fields (reservability, first-come-first-served, season) it is a
review item; member points more than 1 km from the chosen geometry are a `geometry_conflict`
(RIDB's Crane Flat point is 4.4 km from the OSM polygon); conflicting amenity values are
recorded too (OSM marks a dump station at Lower and North Pines, NPS says there is none).

**Manual fixes are versioned rules.** `poi review queue` walks gray-zone pairs and conflicts
and writes decisions to `data/overrides.yaml`: pair decisions (`match` / `non_match`, applied
as must-link / cannot-link in clustering) and field values (`amenities.showers` style paths
for single amenities). Overrides reference records, not entities, so they survive rebuilds
and merges, and they are re-applied on every run.

**Quality score** = 0.4 completeness (8 key fields) + 0.2 corroboration (number of sources) +
0.2 freshness (missed runs, stale seasons) + 0.2 validity (error and warning flags). It is
stored with its components and the list of missing fields, used for search ranking, and
broken down in `reports/quality.md`.

**The quality report names the next source to add.** Coverage gaps are broken down by what
backs an entity (NPS, a RIDB operator, OSM only). On the sierra data, season dates are known
for every NPS-backed campground and for none of the Forest Service or OSM-only ones: the
official-website extraction stage is aimed at exactly that gap.

## Responsible crawling

Seeds are the website URLs of active campground entities, and they are untrusted input:
canonicalized, then screened before any request. Screening rejects non-http(s) schemes, IP
literals and hosts that resolve to private, loopback or link-local addresses (SSRF), social
media, aggregators and review sites, the API sources' own domains (nps.gov, recreation.gov),
and booking engines, which are recorded as an attribute instead of being followed. Every
seed, crawled or not, gets a `sites` row with its outcome or skip reason.

Politeness is configuration (`config/crawl.yaml`): robots.txt obeyed, an honest User-Agent
with a contact URL, one request per host at a time, a download delay plus AutoThrottle,
timeouts and a size cap. `Retry-After` on 429/503 is honoured (capped), with exponential
backoff otherwise. A captcha or block page stops the whole host: a per-host lock in a
downloader middleware makes sure no queued request slips out after the block page arrives.
Nothing is ever worked around.

Scope: same registrable domain; on multi-location hosts (fs.usda.gov, chains), the seed's
path subtree, or its identifying query parameters when the path is generic. At most 6 pages
per site and depth 2, spent on the most relevant links (rates, sites, amenities, season...);
blog, events, cart, login and calendar/date links are never followed. Pages are keyed by
their canonical URL (dedup) but fetched at their real URL: requesting the canonical form
(without `www`) only bounces off a redirect, a bug the first real crawl exposed.

Every attempt is a `pages` row (outcome, flags, status, render mode, content hash) and every
body is stored gzipped in the raw store. Flags: soft 404, parked domain, off-domain redirect
(with booking platform), non-HTML (PDFs stored for later), blocked, robots-blocked,
needs-render (thin static text and no embedded JSON). Re-crawls send `If-None-Match` /
`If-Modified-Since`; a 304 or an identical content hash means no re-extraction.

## Reduction: making the LLM input small without losing facts

Each crawled page is kept in four representations, each with a token count: raw HTML,
cleaned HTML (scripts, styles, navigation, footers, forms, media, cookie banners, comments,
base64 blobs and attributes removed; phone, email and reservation links kept), Markdown, and
plain text. Markdown comes from trafilatura's main-content extraction with tables and lists
kept, falling back to converting the whole cleaned page when the main-content pass keeps too
little.

Main-content extraction drops headers and sidebars by design, and that is where phone numbers
live: on the first run only 58% of distinct phone numbers survived. A **fact salvage** pass
appends, once, every line outside the main content that carries a fact signal (price, phone,
email, month and day, hours, check-in, site count). For about 7% more tokens it brings
retention to 97% for prices and phones and 96% for month mentions. This proxy runs on every
page for free; per-field accuracy against a gold set is measured in the extraction stage.

Pages of one site are bundled into one request (amortizing the instructions), each as a
`## [P1] <url>` section so evidence can cite its page. Lines repeated on at least half of a
site's pages (menus, footers) are removed; those carrying a fact are kept once in a
`[SITE]` section. Bundles are capped at 12,000 tokens; only when a bundle is over the cap
are page sections cut by relevance (headings, fact lines and campground keywords first).

Deterministic facts come first and cost nothing: schema.org JSON-LD and microdata of
business types (Campground, LodgingBusiness, LocalBusiness, Resort...; events are ignored),
`tel:` links normalized to E.164, `mailto:` links (role mailboxes only: personal addresses
are dropped), and `<address>` blocks.

RIDB facility descriptions are documents of their own (`ridb_description`, cited as `[R1]`),
kept apart from crawled pages (`web_bundle`), so each input's contribution and cost can be
reported separately.

Token counts here are estimates (UTF-8 bytes / 4); the extraction stage calibrates them with
the API's free `count_tokens` endpoint. Numbers: [reports/tokens.md](../reports/tokens.md).

## LLM extraction: the cost ladder

Status: implemented and tested with fixtures through the real Anthropic SDK (a mock
transport, so the tests check exactly what would be sent); not run live yet.

Every document goes down a ladder; each step is cheaper than the next, and every extracted
value records which step produced it:

0. **Skip.** Same content hash and prompt version as an existing extraction: reuse it.
1. **Deterministic.** JSON-LD/microdata, `tel:`/`mailto:` links and `<address>` are found
   without a model; they fill gaps (a single unambiguous phone) and cross-check the model.
2. **Reduce.** Markdown with salvaged facts, boilerplate removed, one capped bundle per site.
3. **Cheap model.** Claude Haiku with structured outputs (`output_config.format`, schema with
   `additionalProperties: false`), thinking off, the static instructions as a cached prefix.
4. **Escalate once** to Claude Sonnet when the output is invalid, refused, truncated, or more
   than a quarter of its checkable values are ungrounded.
5. **Human review** when the strong model's answer is still not good enough.

Guardrails: the document is untrusted data inside `<document>` tags (and cannot close them);
the instructions say never to follow text found in it, and the output is schema-validated and
never executed. **Grounding** is deterministic and free: phones, emails, prices, site counts and
lengths must appear in the input or are dropped (counted as hallucinations); evidence quotes
must match the text; fields without grounded evidence are kept as unsupported with low
confidence. Confidence is computed from these checks and from agreement with the deterministic
facts, never asked of the model.

Cost control: every call goes to the `llm_calls` ledger (input, output, cache read/write and
thinking tokens, list-price cost, latency, outcome). `poi extract --dry-run` counts tokens with
the provider's free `count_tokens` endpoint and prices four variants (sync without and with
prompt caching, batch with caching, escalation reserve) before anything is spent;
`--max-cost-usd` stops a run before the projected spend passes the cap; `--batch` sends the
cheap step through Message Batches (half price, 1-hour cache TTL). Responses are recorded by
`hash(provider, model, instructions, input)`, so re-runs, tests and evaluations cost nothing.
Output is kept lean on purpose (unknown fields omitted, short enums, evidence quotes of at most
15 words for key fields): output tokens cost five times more than input tokens on Haiku.

The provider layer is a small protocol with one adapter per provider; an optional Gemini
adapter exists, but Claude is the configured provider.

Evaluation: 18 synthetic gold pages, one per extraction edge case, with expected values
correct by construction and fields that must stay empty (hallucination and privacy bait).
`poi eval extraction` scores per-field accuracy, page-kind accuracy, recall, empty-field rate
and bait violations; `poi eval ablation` sends the same pages as raw HTML, cleaned HTML,
Markdown and plain text to measure tokens against accuracy.

## Serving

Postgres + PostGIS is the system of record; Elasticsearch is a derived read model. `poi index`
builds a new versioned index from the active entities and swaps the alias in one atomic call,
keeping the previous version for rollback; searches never see a half-built index.

Search ranks by text relevance (fuzzy name and alternative names, prefix autocomplete) ×
quality score × a Gaussian distance decay (half score at 25 km) when a location is given, with
filters for amenities, month open, category and radius. A "seasonal" amenity counts as
available (the campground is open in season anyway); "partial" does not, so a filter never
over-promises. Along-route queries stay in PostGIS (`ST_DWithin` against the route line,
ordered by `ST_LineLocatePoint`), which is the road-trip question: what can I reach from the
road, in driving order. A merged id answers 301 with the surviving id, a retired one 410.

The same queries are exposed to assistants through an MCP server (`poi mcp`, stdio). HTTP API
and MCP tools call one query layer (`poi.serve.service`), so both front ends return the same
models and cannot drift. The tools are read-only and say so in their annotations; their
parameter descriptions carry the things a model gets wrong, such as `[lon, lat]` order and the
allowed amenity and category values. `get_campground` follows merge redirects itself (a tool
call has no 301), and retired or unknown ids come back as tool errors with the reason.

## What I would do at 1M POIs

- **LLM-induced selectors for templated sites.** Chains, state park systems and booking
  platforms render thousands of pages from one template. Ask the model once per template for
  CSS selectors (validated against a few pages), extract the rest deterministically, and
  re-learn the selectors when validation starts failing. The LLM cost then scales with the
  number of templates, not pages.
- **Embeddings, revisited at scale.** In this region they did not help (see entity
  resolution), but with many sources and languages, renames and records without coordinates
  become common enough to be worth a candidate source: an ANN index instead of the exact
  matrix product, a gold stratum sampled from embedding-only pairs, and the ablation re-run.
- **Active learning for the review queue.** Send reviewers the pairs whose label would move the
  thresholds most (near the decision boundary, in under-represented strata), and refit the
  weights from accumulated labels (Fellegi–Sunter m/u by EM, or a small classifier on the same
  features).
- **Re-crawl by staleness × popularity.** Conditional requests already make unchanged pages
  cheap; scheduling decides which pages to ask about at all: popular campgrounds with
  seasonal facts first, before the season starts.
- **Batch everything that is not urgent**, keep the instructions as a cached prefix, and watch
  the ledger: cost per page, cache-hit share and escalation rate as alarms (see aws.md).

## Edge cases found in real data

Logged as they are discovered; each handled case has a test.

| Source | What happened | How it is handled |
|---|---|---|
| RIDB | Offset pagination over an unordered result set is unstable. The CA listing returned 1,936 rows but only 1,887 unique facility IDs (NV: 184 rows, 148 unique), so some facilities never appear. Upper Pines (Yosemite) was one of them. | Union of three listings (state, radius around the bbox centre, each parent rec area's facilities), deduplicated by facility ID. Per-strategy yield and skipped rows are recorded in run stats. |
| RIDB | The radius search returned 39 facilities whether the radius was 60 or 100 miles. | Used only as one of the three strategies, never alone. |
| RIDB | 16 CA campgrounds have no coordinates; the embedded parent rec area has an ID and name but no coordinates either. Some are aggregate pseudo-facilities ("Mi Wok Ranger District Campgrounds"). | Kept when their rec area also holds located facilities inside the region, with null geometry and `_region_by = "recarea"`. Large national forests extend beyond the bbox, so this can over-include; normalization flags these records instead of dropping them. |
| RIDB | Many names are ALL CAPS ("LOWER TWIN LAKES AT BRIDGEPORT"). | Smart title case for display (47 names); the raw value is kept. |
| OSM | `out tags geom` returns relations without members, so multipolygon campgrounds (e.g. Tuolumne Meadows) had no geometry. | Query uses `out body geom`; relations are assembled as outer-minus-inner polygons. Test pins the query. |
| OSM | 65 of 289 camp sites have no name (often dispersed or backcountry sites). | Kept; they match on geometry and category only, and normalization flags them. |
| OSM | "Lair of the Golden Bear" is tagged `tourism=camp_site` but is a family/alumni camp, not a public campground. | Excluded as `organized_camp` (operator is a university). |
| RIDB | Two campgrounds claim a Nevada address but lie in California: the address is the national forest's office. 21 addresses are P.O. boxes. | `outside_claimed_state` and `po_box_address` flags; addresses are never used as locations. |
| RIDB | Two records have 2-decimal coordinates (about 1 km), including Tuolumne Meadows. | `low_precision` flag with `precision_m`; ER widens the search radius for them (M3). |
| RIDB | "Pinecrest Day Use Area" is listed with facility type Campground; one state is spelled "CALIFORNIA". | Day-use names are excluded even from trusted listings; full state names map to codes via admin-1 names. |
| RIDB | Three aggregate pseudo-facilities ("Mi Wok Ranger District Campgrounds", "Campgrounds - Highway 108 Corridor"). | Excluded as `aggregate_facility`. |
| RIDB | Email fields contain placeholders such as "N/A". | Treated as absent, not as invalid. |
| OSM | 6 organized camps (`leisure=summer_camp`, or run by a university, church or city: Lair of the Golden Bear, Camp Wawona, San José Family Camp) and 5 cabin-only lodges are tagged `tourism=camp_site`. | Excluded with reason `organized_camp` / `lodging_only` and a warning flag for review. |
| OSM | Renames are recorded in `name_old` / `was:name` ("Camp 4" was "Sunnyside Walk-in Campground"; "AutoCamp Yosemite" was a KOA). | Kept as `alt_names`, used by search and matching. |
| OSM | Operator values contain typos ("Bureau of Land Managment", "Californa State Parks"). | Operator type uses tolerant patterns; the raw operator name is kept. |
| Web | fs.usda.gov answers an identified crawler with "The request is blocked" (403), even for robots.txt. 61 of the 78 crawlable seeds are on that host. | The host is stopped after the first block page and recorded as `blocked`; no workaround. The Forest Service gap is addressed from RIDB's own facility descriptions instead (see the extraction stage). |
| Web | Most official sites redirect `example.com` to `www.example.com`. Requesting the canonical form (no `www`) made every seed "redirect to itself". | Canonical URL for identity, real URL for fetching; same-page and in-scope redirects are followed, a seed that moved within its domain moves the site's scope. |
| Web | Scrapy's `DepthMiddleware` owns the `depth` request meta key and increments it for any request yielded from a callback, so a followed redirect looked one level deeper. | The crawler uses its own `poi_depth` key; the end-to-end test covers a redirected seed. |
| NPS | Structured season dates sometimes disagree with the narrative text (Crane Flat: data opens May 22, text says "mid-June"). | Both kept (`season`, `season_note`); arbitration belongs to survivorship and the LLM stage. |
| NPS | Phone numbers are formatted `209/372-xxxx`. A first version of the contact scrubber and of the publication gate did not recognise `/` as a separator, and the first phone normalizer split such numbers in two. | All three treat a bare `/` as part of the number; values under phone-like keys are scrubbed whatever their formatting. |
