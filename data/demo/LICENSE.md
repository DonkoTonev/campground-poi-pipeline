# Demo data: sources, licenses, changes

`source_records.jsonl.gz` is a snapshot of structured records for the `sierra` region, used by
`make demo` so the pipeline runs without API keys or network access.

## Sources and licenses

- **OpenStreetMap** records (`source = osm`): © OpenStreetMap contributors. Available under
  the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
  This snapshot is a derived database and is itself made available under the ODbL; see
  <https://www.openstreetmap.org/copyright>.
- **Recreation Information Database** records (`source = ridb`): Recreation.gov RIDB API,
  U.S. federal government data.
- **NPS API** records (`source = nps`): U.S. National Park Service, federal government data.
- `data/reference/admin1_us_ca.geojson`: Natural Earth admin-1 boundaries, public domain.

## Changes made to the original records

- Phone numbers are replaced with fictional 555-01xx numbers and email addresses with
  `example.com` addresses (`poi.privacy.scrub`). The mapping is deterministic, so records that
  shared a number still share one.
- Bulky fields not used by the pipeline (images, media, events, tours, per-campsite lists) were
  dropped at ingest time.
- No content crawled from campground websites is included.

Regenerate with `uv run python scripts/export_demo.py` after `poi ingest`.
