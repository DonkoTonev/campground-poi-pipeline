# campground-poi-pipeline

A small, production-minded data pipeline for campground points of interest. It aggregates
records from public sources (OpenStreetMap, Recreation.gov RIDB, the NPS API) and official
campground websites, uses an LLM only where it adds value and as cheaply as possible, resolves
duplicates (including near neighbours that are *not* duplicates), builds golden records with
field-level provenance, measures its own quality and cost, and serves the result through
search and an API. Depth and correctness over breadth: one region (Yosemite and the Eastern
Sierra), where federal, state, county and private campgrounds sit close together.

> **Status.** Built end to end and measured on real data, including one live LLM extraction
> run (Claude Haiku 5.5, escalating to Sonnet 5.5) on 2026-11-25. Entity-resolution labels are
> Claude-drafted; a person verified 50 of them (56% raw agreement) and adjudicated the 22
> disagreements. Progress: [docs/PLAN.md](docs/PLAN.md).

## Architecture

```mermaid
flowchart LR
  subgraph sources [Sources]
    OSM[OpenStreetMap<br/>Overpass]
    RIDB[Recreation.gov<br/>RIDB API]
    NPS[NPS API]
    WEB[Official websites]
  end
  OSM & RIDB & NPS --> ING[ingest] --> RAW[(raw store<br/>local / S3)]
  ING --> SR[(source_records)]
  SR -->|website seeds| CR[crawl<br/>Scrapy, robots.txt] --> RAW
  CR --> RED[reduce<br/>HTML → Markdown,<br/>deterministic facts]
  RIDB -.->|facility descriptions| RED
  RED --> EXT[extract<br/>Haiku → Sonnet,<br/>grounding, ledger]
  EXT -->|web / ridb_description| SR
  SR --> NORM[normalize<br/>+ quality flags] --> MATCH[match<br/>blocking, log-odds,<br/>constrained clusters]
  MATCH --> RES[resolve<br/>stable ids, survivorship,<br/>overrides]
  RES --> PG[(Postgres + PostGIS<br/>golden records)]
  PG --> IDX[index<br/>alias swap] --> ES[(Elasticsearch)]
  PG & ES --> API[FastAPI]
  PG --> REP[reports: quality,<br/>tokens, cost, changes]
  REV[review CLI] -->|data/overrides.yaml| MATCH & RES
```

Every stage is a `poi <stage>` command, idempotent, and records a `runs` row with its
parameters and stats. Raw responses are stored once and reprocessed many times.

Terms used below:
- **Entity resolution (ER):** deciding which records from different sources describe the same
  campground. **Blocking** picks the candidate pairs worth comparing (nearby, similar name,
  shared id); each pair gets a log-odds score from named features (Fellegi–Sunter style).
  Pairs between the merge and reject thresholds (the **gray zone**) go to human review.
- **Golden record:** the one merged record per real campground. **Survivorship** picks each
  field's value from the members by source trust and agreement, keeping the alternatives.
- **Alias swap:** the search index is rebuilt under a new name, then the alias the API reads
  is switched to it in one step.

## Quick start: `make demo`

Needs Docker (with about 3 GB of memory: Elasticsearch takes a 1 GB heap) and
[uv](https://docs.astral.sh/uv/). No API keys, no crawling. Ports 5433 (Postgres), 9200
(Elasticsearch) and 8000 (API) must be free; set `POI_DB_PORT` to move Postgres.

```bash
git clone https://github.com/DonkoTonev/campground-poi-pipeline && cd campground-poi-pipeline
make demo
```

It starts PostGIS and Elasticsearch, loads a committed snapshot of the structured records
(contact data scrubbed, see [data/demo/LICENSE.md](data/demo/LICENSE.md)) into its own
database, runs normalize → match → resolve → index, prints example queries and starts the API
on <http://127.0.0.1:8000/docs>. On a fresh clone with warm package and image caches, loading,
normalizing, matching, resolving and indexing took about 3 seconds (measured 2026-11-24); a
first run is dominated by downloading the Docker images.

What you should see: a summary (394 records → 300 entities, 60 confirmed by more than one
source) and example URLs such as `/campgrounds/search?q=tuolume%20medows` (typo on purpose)
and an along-route request. Running `make demo` again is safe: the snapshot is reloaded, the
pipeline is idempotent and the search index is rebuilt and swapped. Ctrl-C stops the API;
`make down` stops the containers (the data volumes stay; `docker compose down -v` removes
them too).

## Full run (with API keys)

```bash
make setup                 # uv sync + git hooks
cp .env.example .env       # RIDB_API_KEY, NPS_API_KEY, ANTHROPIC_API_KEY, CONTACT_URL
make up migrate
make ingest normalize match resolve      # structured sources → golden records
uv run poi crawl --plan && make crawl     # official websites (robots.txt, 1 request/host)
make reduce                               # pages + RIDB descriptions → LLM input documents
uv run poi extract --dry-run              # free token count and cost estimate, nothing generated
uv run poi extract --max-cost-usd 2       # Haiku, escalation to Sonnet; add --batch for 50% off
make normalize match resolve index report
make serve
```

## Review and overrides

Human-in-the-loop: `uv run poi review label` verifies the random sample of entity-resolution
gold pairs blind, `uv run poi review adjudicate --by <name>` decides the pairs where that label
and the draft disagree (a separate final label);
`uv run poi review queue` decides gray-zone pairs and field conflicts and writes the decisions
to `data/overrides.yaml`, which every run applies.

## Results

Measured on the `sierra` region on 2026-11-24. Every number comes from a pipeline run; the
reports in [reports/](reports/) are generated from the database.

**Sources → records.** One Overpass query, 54 RIDB pages, one NPS request; a second ingest makes
0 network requests (raw store). Request counts are from the ingest runs' stats (`runs` table).

| Source | Records in region | With coordinates | With polygon |
|---|---|---|---|
| OpenStreetMap | 289 | 289 | 158 |
| Recreation.gov RIDB | 89 | 82 (+7 placed by their rec area) | – |
| NPS API | 16 | 16 | – |

RIDB's offset pagination silently skips facilities (CA: 1,936 rows, 1,887 unique ids), so the
adapter combines three listings: the state listing alone found 68 of the 82 located
campgrounds.

**Normalization** (structured sources). 379 of 394 records are campgrounds; 15 are excluded and flagged (6 organized
camps, 5 cabin-only lodges, 1 day-use area, 3 aggregate pseudo-facilities). 47 ALL-CAPS names
re-cased, 21 P.O.-box addresses, 7 records with (0,0) coordinates, 2 low-precision points and
2 records whose claimed state contradicts their point, all as flags, none fixed silently.
65 records have no name (unnamed OSM sites); 78% of entities have one.

**Entity resolution.** The structured sources alone: 379 campground records → 300 entities
(79 duplicates merged, 60 entities confirmed by more than one source). With the 104 records the
LLM extracted (19 from websites, 85 from RIDB descriptions) joining the same matching: 483
records → **304 entities**, 97 confirmed by more than one source; 78 pairs in the review queue;
5 group or horse areas attached to a parent campground. The extracted records also merged 3
pairs the structured sources had kept apart (Crags, Coldwater, Sweetwater: the same campground
in OSM and RIDB, bridged by its RIDB description; checked by hand). Upper, Lower and North Pines
(within ~300 m) stay three entities.

Labels: Claude-drafted labels (200 pairs, drafted blind to the matcher's score and
decision), 50 human-verified, **agreement 56% (28/50)** before adjudication. All 22
disagreements went one way: the person said match or unsure where the draft said non-match or
unsure (11 non-match vs match, 8 non-match vs unsure, 3 unsure vs match). The labelling screen
had not defined "match"; it now does (same real-world campground, one pin).

After adjudication (`poi review adjudicate`, a third label; both originals are kept): the
final label sides with the draft on 16 of the 22, with the person's first label on 5, and with
neither on 1. Against the final labels the draft is right on 44 of 50 sample pairs (88%), the
person's first labels on 33 of 50 (66%). Four of the five pairs settled as match are related
facilities the matcher deliberately keeps as separate entities (two horse camps next to their
family campground, two lettered loops, a backpackers' campground next to the main one), so the
final labels count them as matches the matcher misses. [reports/er_eval.md](reports/er_eval.md)
lists all 22 with the draft, its rationale, the first human label and the final label.

Matcher precision and recall depend on which labels are taken as truth, so all are shown
(counting a pair as merged when both records end up in one entity; Wilson 95% CIs):

| labels taken as truth | pairs | precision | recall |
|---|---|---|---|
| Claude-drafted (all decisive pairs) | 189 | 1.00 (0.92–1.00) | 0.87 (0.75–0.93) |
| human, before adjudication (decisive sample pairs) | 42 | 1.00 (0.80–1.00) | 0.50 (0.33–0.67) |
| **final, after adjudication** (decisive sample pairs) | 47 | **1.00 (0.80–1.00)** | **0.75 (0.53–0.89)** |
| drafted, corrected by adjudication (all decisive pairs) | 189 | 1.00 (0.92–1.00) | 0.80 (0.68–0.89) |

No label set finds a false merge. The missed matches are name variants ("Agnew Meadows Horse
Campground" vs "Agnew Horse Camp", a miss under every label set), one OSM campground that two
RIDB ids both claim (the never-merge-two-RIDB-ids constraint keeps it apart), and, under the
adjudicated labels, the related facilities above. Whether those should be one entity or a
parent with children is a product decision; the matcher currently links some of them as
sub-areas (Bridalveil Creek Horse Camp is a child of Bridalveil Creek Campground).

Name embeddings (a local model: `uv sync --extra embeddings`, then `poi match --embeddings`)
were tried as a candidate source and as a matching feature, and are off: as candidates they
changed no decision, as a feature they traded 2 found matches for 2 false merges
(F1 0.928 → 0.931, within noise;
[reports/er_embeddings.md](reports/er_embeddings.md)).

**Golden records.** Each field is chosen by per-field source trust plus corroboration, with
every alternative and its provenance kept. Mean quality score: 0.57 for single-source
entities, 0.80 for two sources, 0.89 for three, 0.98 for four. Conflicts flagged instead of guessed:
RIDB's Crane Flat and Wawona points are 3–4 km from where OSM and NPS put them; OSM and NPS
disagree on dump stations at two Pines campgrounds.

**Data gaps** ([reports/quality.md](reports/quality.md)): even after extraction, season dates
are known for 11% of entities: all NPS-backed campgrounds have them, 6% of the Forest Service
ones and 6% of the 219 OSM-only ones. OSM-only campgrounds have no website to read, and the
Forest Service blocks identified crawlers, so the report names the next source for each gap.

**Crawling** (stats of the crawl run in the `runs` table). 121 candidate URLs → 78 crawlable seeds after screening (30 on API domains,
10 booking engines, 2 aggregators, 1 unofficial site skipped). 15 official sites crawled
(77 responses: 76 HTML pages and 1 XML document, about 5 per site), 2 dead seeds. 61 seeds are on fs.usda.gov, which answers an
identified crawler with a block page: the crawler stopped after one request and did not work
around it. The same Forest Service descriptions arrive through the RIDB API, which is used as
a separate extraction input (`ridb_description`). No page needed a headless browser.

**Reduction on real crawled pages** ([reports/tokens.md](reports/tokens.md)), no LLM involved.
Token counts are bytes/4 estimates; the ratios between representations are what matters
here:

| Representation | Tokens per page | Share of raw HTML |
|---|---|---|
| raw HTML | 36,437 | 100% |
| cleaned HTML | 2,133 | 5.9% |
| Markdown + salvaged facts | 963 | 2.6% |
| plain text | 943 | 2.6% |

On the first reduce run, before fact salvage existed, main-content extraction alone kept only
58% of distinct phone numbers (they live in headers and footers; that run predates the
report); a fact-salvage pass brings it to 97% for prices and phones and 96% for months, for
about 7% more tokens. **On real pages, the Markdown the model reads is about 2.6% of the raw
HTML.** The LLM inputs are 15 website bundles and 85 RIDB description documents. The API's
`count_tokens` shows the bytes/4 estimate is low for this content: about 6,450 real tokens per
bundle against 4,370 estimated (1.48×), and 1,050 against 760 per RIDB description (1.38×),
before the fixed prompt (instructions and schema, 4,097 tokens, cached after the first call).

**LLM extraction** (live run, 2026-11-25; [reports/cost.md](reports/cost.md)). 100 documents,
Haiku first, Sonnet when the output fails validation or grounding:

| | RIDB descriptions | Website bundles | All |
|---|---|---|---|
| documents | 85 | 15 | 100 |
| finished by Haiku | 71 | 12 | 83 |
| escalated to Sonnet | 14 | 3 | 17 |
| sent to human review | 5 | 1 | 6 |
| billed (current extractions) | $0.117 | $0.086 | **$0.20** |

That is about $2 per 1,000 extracted campgrounds (104 extracted) at list prices with prompt
caching (60% of input tokens were cache reads) and synchronous calls; the Batch API would halve
it. Every value is grounded in the input text; values that are not are dropped, and documents
with too many of them go to review (mean ungrounded share there 66–80%, 0–0.6% in accepted
ones). What the extraction added to the golden records (share of entities with a value, before
→ after): season dates 5% → 11%, price 5% → 9%, phone 22% → 27%, drinking water 12% → 28%,
showers 8% → 15%. In the chosen values, websites supplied 14 seasons, 10 prices and 17 phones;
RIDB descriptions 4, 1 and 3; 76% of RIDB description records state whether there is
drinking water. All extraction spending so far, including the evaluation runs and a first attempt
that failed: $0.36 (ledger).

**Extraction accuracy** ([reports/extraction_eval.md](reports/extraction_eval.md)) on 18
synthetic gold pages, one per edge case: page kind 100%, campground recall 100%, field accuracy
96% (44 of 46 fields; the misses are both prices on the French/CAD page), 0 violations on the
hallucination, prompt-injection and personal-name bait, 1 escalation. Real-page accuracy is not
measured: real-page gold labels would contain third-party content.

**Representation ablation on synthetic pages** ([reports/ablation.md](reports/ablation.md)),
Haiku only, same 18 pages as raw HTML, cleaned HTML, Markdown and plain text: same field
accuracy (96%) for all four, Markdown with about 15% fewer input tokens than raw HTML (5,010
vs 5,876 per page, instructions included). **This 15% is not the real-page saving:** the
synthetic pages are a few hundred bytes of clean HTML, and most of each request is the fixed
prompt. On real crawled pages the input shrinks to about 2.6% of raw HTML (Reduction, above).
The ablation found a bug first: Markdown dropped a page's leading H1, usually the campground's
name (field accuracy 52% vs 94% for raw HTML); fixed before the numbers above.

**Serving** (search and map from Elasticsearch, along-route and detail from PostGIS).
Typo-tolerant search ("tuolume medows" finds Tuolumne Meadows), filters by
amenity, month open and category, distance decay; along-route queries return campgrounds in
driving order (Big Oak Flat → Yosemite Valley → Tuolumne lists the Valley campgrounds at
km 23–27 of a 72 km route).

## Use it from Claude (MCP)

`poi mcp` is an [MCP](https://modelcontextprotocol.io) server over stdio with three read-only
tools: `search_campgrounds` (fuzzy name, near a point, amenities, month open, category),
`get_campground` (golden record with per-field provenance; merged ids resolve to the
survivor) and `along_route` (campgrounds near a `[lon, lat]` route, in driving order). It
reads the same Postgres and Elasticsearch as the API, so run `make demo` (or the full
pipeline) first.

Claude Code, from the repository root, against the demo database:

```bash
claude mcp add campgrounds \
  -e DATABASE_URL=postgresql+psycopg://poi:poi@localhost:5433/poi_demo \
  -e INDEX_PREFIX=demo-campgrounds \
  -- uv run --directory "$PWD" poi mcp
```

Claude Desktop (`claude_desktop_config.json`), with an absolute path to the clone:

```json
{
  "mcpServers": {
    "campgrounds": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/campground-poi-pipeline", "poi", "mcp"],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://poi:poi@localhost:5433/poi_demo",
        "INDEX_PREFIX": "demo-campgrounds"
      }
    }
  }
}
```

Leave out the two variables to use the development database instead of the demo one, and
change 5433 if you set `POI_DB_PORT`. Then
ask, for example: "Which campgrounds near Tuolumne Meadows are open in July and have drinking
water?" or "I'm driving from Big Oak Flat to Tioga Pass, what can I camp at along the way?"

## Edge cases handled

Matching, normalization and crawling cases have tests; the extraction cases are gold cases,
scored once the LLM stage runs live. Cases found in real data are logged in
[docs/DESIGN.md](docs/DESIGN.md#edge-cases-found-in-real-data).

- **Matching:** distinct campgrounds within ~300 m (Upper / Lower / North Pines); the same
  campground 600 m apart or with a 4 km coordinate error; same name in different places; name
  variants and abbreviations; group and horse camps next to the family campground; group sites
  inside a campground polygon (child, not merge); shared ranger-district phones; shared
  recreation.gov id with different names; OSM node + way duplicates; distinct RIDB ids that
  must never merge; transitive chains; low-precision coordinates; renames (review, old name
  kept).
- **Normalization:** (0,0), swapped and sign-flipped coordinates, out-of-region points, a
  claimed state that contradicts the point, ALL-CAPS names, phone formats and placeholders,
  "Camp …" names that are youth camps, lodges and day-use areas, meters vs feet, NPS seasons
  encoded as recurring closures, holiday-anchored seasons, stale seasons.
- **Crawling:** robots.txt, Retry-After, soft 404, parked domains, off-domain redirects to
  booking engines, `www` redirects, moved seeds, calendar traps, URL duplicates, SSRF seeds
  (private IPs, cloud metadata), social media, captcha / block pages stopping the whole host,
  PDFs.
- **Extraction** (synthetic gold cases): multi-campground pages, no campground, negations, partial and
  seasonal amenities, holiday seasons, past-year seasons, three kinds of closure, price ranges
  with extra fees, CAD and French, prompt injection, hallucination bait, marketing titles,
  parked domains, very long pages, staff names.

## Design decisions and trade-offs

- **Distance generates candidates and is one feature, never a merge rule.** Conflicting
  discriminators ("Upper" vs "Lower", "Horse" vs none) outweigh being 25 m apart; a shared
  recreation.gov id outweighs a 4 km coordinate error.
- **Precision over recall in matching.** A false merge deletes a real campground from the app;
  a false split shows a duplicate pin. Gray-zone pairs go to review.
- **Explainable scoring.** Fellegi–Sunter-style log-odds with hand-set weights; every decision
  stores its per-feature contributions.
- **Nothing is fixed silently.** Every rule violation is a `quality_flags` row; unknown is
  never "no"; manual fixes are versioned rules in `data/overrides.yaml`.
- **Stable ids.** Entities follow record overlap across runs; merges leave redirects (the API
  answers 301), nothing is deleted.
- **The LLM sees as little as possible.** Deterministic facts first, Markdown at 2.6% of raw
  HTML, one request per site, cheap model first, one escalation, every value grounded in the
  input, confidence computed rather than asked for, every call in a cost ledger with a
  dry-run estimate and a hard cost cap.
- **Postgres is the system of record**, Elasticsearch a rebuildable read model behind an alias.

Deeper rationale, including what changes at 1M POIs: [docs/DESIGN.md](docs/DESIGN.md).
Production sketch on AWS: [docs/aws.md](docs/aws.md).

## Limitations and next steps

- Decide whether related facilities (horse camps, loops, backpackers' sites) are one entity or
  children of one, then the merge threshold. Pair scores alone on the drafted labels
  (before clustering, so recall differs from the 0.87 above): at 4, the current setting,
  precision 1.00 / recall 0.88; at 2, precision 0.96 / recall 0.92.
- One region; Forest Service websites are blocked for identified crawlers, so their facts come
  only from RIDB descriptions.
- Token counts in the reduction report are bytes/4 estimates; `count_tokens` shows they are
  about 1.4–1.5× low for this content (see LLM extraction). The ratios between representations
  hold.
- The constrained-output mode of the API cannot compile the extraction schema (60 optional
  fields), so the schema travels in the prompt and every reply is validated locally; 11 of 135
  live replies failed validation and were escalated.
- Some stage functions (`resolve_region`, the report builders) are long; they read top to
  bottom as one stage each, but would be split before the code grows further.
- Real-page extraction gold labels are not committed (third-party content); the committed gold
  set is synthetic.

## Data sources and attribution

- Map data © OpenStreetMap contributors, available under the Open Database License (ODbL).
  The demo snapshot contains OSM-derived records and is made available under the ODbL.
- Recreation Information Database (RIDB), Recreation.gov.
- U.S. National Park Service API.
- State and province boundaries: Natural Earth (public domain).
- No commercial aggregators or review sites are crawled.

## How I built this

I used Claude Code as a pair programmer for planning, implementation and review. I owned the
design decisions and the verification: what to measure, what counts as a correct match, which
trade-offs to make, and checking the numbers against the data.
