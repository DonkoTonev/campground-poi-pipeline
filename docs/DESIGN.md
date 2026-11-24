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
similar names that blocking missed). Suggested labels come from the model and are marked
unverified; `poi review label` hides them by default to avoid anchoring the reviewer.
`poi eval er` reports precision and recall on verified labels only, with Wilson intervals.

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
| NPS | Structured season dates sometimes disagree with the narrative text (Crane Flat: data opens May 22, text says "mid-June"). | Both kept (`season`, `season_note`); arbitration belongs to survivorship and the LLM stage. |
| NPS | Phone numbers are formatted `209/372-xxxx`. A first version of the contact scrubber and of the publication gate did not recognise `/` as a separator, and the first phone normalizer split such numbers in two. | All three treat a bare `/` as part of the number; values under phone-like keys are scrubbed whatever their formatting. |
