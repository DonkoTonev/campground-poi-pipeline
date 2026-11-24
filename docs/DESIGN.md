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

## Edge cases found in real data

Logged as they are discovered. Handled cases have tests; items marked M2 get theirs there.

| Source | What happened | How it is handled |
|---|---|---|
| RIDB | Offset pagination over an unordered result set is unstable. The CA listing returned 1,936 rows but only 1,887 unique facility IDs (NV: 184 rows, 148 unique), so some facilities never appear. Upper Pines (Yosemite) was one of them. | Union of three listings (state, radius around the bbox centre, each parent rec area's facilities), deduplicated by facility ID. Per-strategy yield and skipped rows are recorded in run stats. |
| RIDB | The radius search returned 39 facilities whether the radius was 60 or 100 miles. | Used only as one of the three strategies, never alone. |
| RIDB | 16 CA campgrounds have no coordinates; the embedded parent rec area has an ID and name but no coordinates either. Some are aggregate pseudo-facilities ("Mi Wok Ranger District Campgrounds"). | Kept when their rec area also holds located facilities inside the region, with null geometry and `_region_by = "recarea"`. Large national forests extend beyond the bbox, so this can over-include; normalization flags these records instead of dropping them. |
| RIDB | Many names are ALL CAPS ("LOWER TWIN LAKES AT BRIDGEPORT"). | M2: smart title case in normalization; the raw value is kept. |
| OSM | `out tags geom` returns relations without members, so multipolygon campgrounds (e.g. Tuolumne Meadows) had no geometry. | Query uses `out body geom`; relations are assembled as outer-minus-inner polygons. Test pins the query. |
| OSM | 65 of 289 camp sites have no name (often dispersed or backcountry sites). | Kept; they match on geometry and category only, and normalization flags them. |
| OSM | "Lair of the Golden Bear" is tagged `tourism=camp_site` but is a family/alumni camp, not a public campground. | M2: non-campground detection in normalization. |
| NPS | Phone numbers are formatted `209/372-xxxx`. A first version of the contact scrubber and of the publication gate did not recognise `/` as a separator. | Both accept `/`; values under phone-like keys are replaced whatever their formatting. |
