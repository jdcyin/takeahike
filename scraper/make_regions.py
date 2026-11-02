"""Build data/geo/hk_regions.geojson: Hong Kong's 18 districts merged into
the site's six areas (HK Island, Kowloon, NT East, NT West, Lantau, Islands).

Source: District Council boundaries GeoJSON
https://github.com/UnKnoWn-Consortium/DC2015_JSON (geojson.json)

Usage
  python scraper/make_regions.py path/to/geojson.json
"""

import json
import sys
from pathlib import Path

from shapely.geometry import MultiPolygon, mapping, shape
from shapely.ops import unary_union
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "geo" / "hk_regions.geojson"

GROUPS = {
    "hk_island": ["Central and Western", "Wan Chai", "Eastern", "Southern"],
    "kowloon": ["Yau Tsim Mong", "Sham Shui Po", "Kowloon City", "Wong Tai Sin", "Kwun Tong"],
    "nt_west": ["Tsuen Wan", "Tuen Mun", "Yuen Long", "Kwai Tsing"],
    "nt_east": ["North", "Tai Po", "Sha Tin", "Sai Kung"],
    "islands_district": ["Islands"],
}
NAMES = {
    "hk_island": "Hong Kong Island", "kowloon": "Kowloon", "nt_east": "New Territories East",
    "nt_west": "New Territories West", "lantau": "Lantau", "islands": "Islands",
}


def parts(g):
    return list(g.geoms) if isinstance(g, MultiPolygon) else [g]


def main(src):
    data = json.load(open(src, encoding="utf-8"))
    wanted = {n for v in GROUPS.values() for n in v}
    districts = {}
    for f in data["features"]:
        name = f["properties"].get("ename")
        if name not in wanted or name in districts:
            continue
        g = f["geometry"]
        geoms = g["geometries"] if g["type"] == "GeometryCollection" else [g]
        districts[name] = unary_union([make_valid(shape(x)).buffer(0) for x in geoms]).buffer(0)

    regions = {k: unary_union([districts[d] for d in v]).buffer(0) for k, v in GROUPS.items()}
    # Islands District = Lantau (plus the airport, joined to it) + outlying islands.
    isl = parts(regions.pop("islands_district"))
    lantau = max(isl, key=lambda p: p.area).buffer(0.004)
    regions["lantau"] = unary_union([p for p in isl if p.intersects(lantau)])
    regions["islands"] = unary_union([p for p in isl if not p.intersects(lantau)])

    features = []
    for key, geom in regions.items():
        geom = geom.simplify(0.0004, preserve_topology=True)
        features.append({"type": "Feature", "properties": {"id": key, "name": NAMES[key]}, "geometry": mapping(geom)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
