"""Build data/reference/admin1_us_canada.geojson from Natural Earth admin-1 (public domain).

Usage: uv run python scripts/build_admin1.py <ne_10m_admin_1_states_provinces.geojson>
Source: https://www.naturalearthdata.com/ (10m cultural, admin 1 states/provinces).
Keeps US and Canada, simplifies to ~0.005 degrees, rounds to 4 decimals. The state check
in normalization applies a tolerance buffer, so this precision is sufficient.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from shapely.geometry import mapping, shape

from poi.settings import PROJECT_ROOT

OUT = PROJECT_ROOT / "data" / "reference" / "admin1_us_canada.geojson"
TOLERANCE_DEG = 0.005


def _round(obj: object) -> object:
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, list | tuple):
        return [_round(v) for v in obj]
    return obj


def main(src: Path) -> None:
    features = []
    for feat in json.loads(src.read_text())["features"]:
        props = feat["properties"]
        if props["iso_a2"] not in ("US", "CA") or not props.get("postal"):
            continue
        geom = shape(feat["geometry"]).simplify(TOLERANCE_DEG, preserve_topology=True)
        gj = mapping(geom)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "country": props["iso_a2"],
                    "code": props["postal"],
                    "name": props["name"],
                },
                "geometry": {"type": gj["type"], "coordinates": _round(gj["coordinates"])},
            }
        )
    features.sort(key=lambda f: (f["properties"]["country"], f["properties"]["code"]))
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}) + "\n")
    print(
        f"wrote {OUT.relative_to(PROJECT_ROOT)}: {len(features)} features, "
        f"{OUT.stat().st_size // 1024} KiB"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
