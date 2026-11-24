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
