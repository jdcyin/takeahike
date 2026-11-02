"""Turn data/trails.json + GPX tracks into the compact file the website loads.

Writes web/data/trails.json with, per trail: area (one of the site's six),
our difficulty rating, a simplified route for the map and an elevation
profile. Also writes web/data/regions.json (SVG paths for the area map).

Usage
  python scraper/build_web_data.py
"""

import json
import math
import re
import sys
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Point, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape_hiking_gov as gov  # noqa: E402  (reuses the GPX helpers)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "web" / "data"

# Our difficulty: "effort km" = distance + 1 km for every 100 m climbed.
BANDS = [(5, "Easy"), (10, "Moderate"), (math.inf, "Hard")]
# Brutal takes length AND climb: sqrt(km * metres climbed), so a long flat walk
# or a short steep one can't reach it. 75 always means at least 15 effort km.
BRUTAL_SCORE = 75
KOWLOON_REACH_DEG = 0.01  # ~1 km: hills right above Kowloon count as Kowloon

# Extra "where is this?" line for names people easily confuse.
PLACE_NOTES = {
    "ping-chau-country-trail": "Tung Ping Chau, in Mirs Bay (not Peng Chau)",
}


def parts(g):
    return list(g.geoms) if isinstance(g, MultiPolygon) else [g]


def load_regions():
    feats = json.load(open(DATA / "geo" / "hk_regions.geojson", encoding="utf-8"))["features"]
    return {f["properties"]["id"]: shape(f["geometry"]) for f in feats}


def assign_area(route, start, regions):
    """Ferry-only islands first, then Kowloon's hills, then wherever it starts."""
    p = Point(start[1], start[0])
    for key in ("nt_east", "nt_west", "hk_island"):
        pieces = sorted(parts(regions[key]), key=lambda g: g.area, reverse=True)
        for part in pieces[1:]:  # everything except the mainland piece
            if part.buffer(0.002).contains(p):
                return "islands"
    if regions["islands"].buffer(0.002).contains(p):
        return "islands"
    if route.distance(regions["kowloon"]) < KOWLOON_REACH_DEG:
        return "kowloon"
    for key, g in regions.items():
        if g.contains(p):
            return key
    return min(regions, key=lambda k: regions[k].distance(p))


# ---------------------------------------------------------------- directions

OPERATOR = (r"(Kowloon Motor Bus \(KMB\)|KMB|Citybus|New Lantao Bus|Long Win Bus|MTR Bus|"
            r"Green [Mm]inibus|Red [Mm]inibus|Public [Mm]inibus|[Mm]inibus|MTR|Ferry Service|Ferry)")
ROUTE_LIST = re.compile(OPERATOR + r"\s*(?:\(Source:[^)]*\)\s*)?(?:routes?)?\s*(?:no\.)?\s*[-–:]\s*")
SHORT_NAMES = {"Kowloon Motor Bus (KMB)": "KMB", "Green minibus": "Green Minibus", "Public minibus": "Minibus",
               "Red minibus": "Red Minibus", "minibus": "Minibus", "Ferry Service": "Ferry"}
MAX_ROUTES = 8
ROUTE_CODE = re.compile(r"^[A-Z]{0,3}\d{1,4}[A-Z]{0,2}$")  # 9, 70P, A17, N8X, K51


def step_mode(text):
    t = text.lower()
    for words, mode in ((("ferry", "pier", "kaito"), "Ferry"), (("mtr",), "MTR"), (("minibus",), "Minibus"),
                        (("bus", "public transport"), "Bus"), (("taxi",), "Taxi"), (("tram",), "Tram")):
        if any(w in t for w in words):
            return mode
    return "Walk"


def split_steps(text, leaving=False):
    """Break the government's 'getting there' paragraph into short steps:
    one per instruction, plus one per list of bus / minibus / MTR / ferry routes
    (flagged "routes", so the site can show the operator's name in front)."""
    if not text:
        return []
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*及\s*", " and ", text)  # a stray "and" left in Chinese on the English pages
    first_list = ROUTE_LIST.search(text)
    prose, lists = (text[:first_list.start()], text[first_list.start():]) if first_list else (text, "")

    # Instructions: sentences, then long sentences split at "and then" / ", then".
    steps = []
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", prose.strip()):
        pieces, carry = [], ""
        for part in re.split(r"(,? and then |, then |, (?=(?:walk|turn|take|follow) ))", sentence):
            if re.fullmatch(r",? and then |, then |, ", part):
                continue
            part = (carry + part).strip(" ,;")
            # "After getting off" is not a step on its own: glue it to the next part.
            if len(part.split()) < 4:
                carry = part + ", "
                continue
            carry = ""
            pieces.append(part)
        if carry and pieces:
            pieces[-1] += ", " + carry.strip(" ,")
        elif carry:
            pieces.append(carry.strip(" ,"))
        for part in pieces:
            if len(part) < 3:
                continue
            part = part[0].upper() + part[1:]
            if not part.endswith((".", "!", "?", ")")):
                part += "."
            steps.append({"mode": step_mode(part), "text": part})

    # Route lists: "Citybus routes - 7, 70P ... Green Minibus routes - 4A ..."
    route_steps = []
    marks = list(ROUTE_LIST.finditer(lists))
    for i, m in enumerate(marks):
        body = lists[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(lists)].strip(" .;,")
        name = SHORT_NAMES.get(m.group(1), m.group(1))
        if name in ("MTR", "Ferry"):
            route_steps.append({"mode": name, "text": body + ("" if body.endswith(".") else "."), "routes": True})
            continue
        routes = [r.strip() for r in re.split(r",| and ", body) if r.strip()]
        if routes and all(ROUTE_CODE.match(r) for r in routes):
            more = f" and {len(routes) - MAX_ROUTES} more" if len(routes) > MAX_ROUTES else ""
            text = ("route " if len(routes) == 1 else "routes ") + ", ".join(routes[:MAX_ROUTES]) + more
        else:
            text = body  # e.g. "Shau Kei Wan to and from Shek O": keep the wording as given
        route_steps.append({"mode": name, "text": text, "routes": True})

    # Station/ferry first; bus route lists right after the step that says to take one.
    first = [s for s in route_steps if s["mode"] in ("MTR", "Ferry")]
    buses = [s for s in route_steps if s["mode"] not in ("MTR", "Ferry")]
    if leaving:
        # Going home: walk out first, then the buses you can catch, then the station or pier.
        steps = steps + buses + first
    else:
        at = next((i + 1 for i, s in enumerate(steps) if s["mode"] in ("Bus", "Minibus")), 0 if not steps else 1)
        steps = first + steps[:at] + buses + steps[at:]
    return steps[:10]


def tidy_finish(text):
    """'Take public transport at X. Citybus route - 9 ...' -> 'Take public transport at X. Citybus route 9; ...'"""
    if not text:
        return text
    parts = split_steps(text)
    prose = " ".join(s["text"] for s in parts if not s.get("routes"))
    routes = "; ".join(f"{s['mode']} {s['text'].rstrip('.')}" for s in parts if s.get("routes"))
    return " ".join(x for x in (prose, routes + "." if routes else "") if x)


def profile(pts, samples=60):
    """Elevation every 1/samples of the distance, smoothed like the scraper."""
    eles = gov.median_smooth([p[2] or 0 for p in pts])
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + gov.haversine_km(pts[i - 1], pts[i]))
    total, out, j = cum[-1] or 1, [], 0
    for k in range(samples + 1):
        target = total * k / samples
        while j < len(cum) - 1 and cum[j] < target:
            j += 1
        out.append(round(eles[j]))
    return out


def build():
    regions = load_regions()
    trails = json.load(open(DATA / "trails.json", encoding="utf-8"))["trails"]
    out = []
    for t in trails:
        pts = []
        for f in t["gpx_files"]:
            pts += gov.gpx_points((DATA / f).read_bytes())
        if len(pts) < 2:
            continue
        track = t["track"]
        line = LineString([(p[1], p[0]) for p in pts])
        gpx_km, official_km = track["distance_km"], t["length_km"] or track["distance_km"]
        out_and_back = official_km > 1.6 * gpx_km  # official length counts the walk back
        ascent = track["ascent_m"] * (2 if out_and_back else 1)
        distance = max(official_km, gpx_km)
        effort = distance + ascent / 100
        if math.sqrt(distance * ascent) >= BRUTAL_SCORE:
            rating = "Brutal"
        else:
            rating = next(name for limit, name in BANDS if effort < limit)
        simple = line.simplify(0.00008)
        loop = gov.haversine_km(pts[0], pts[-1]) < 0.3
        out.append({
            "id": t["id"],
            "name": re.sub(r"\)(?=\w)", ") ", t["name_en"]),  # "(Section 1)Pak Tam" -> "(Section 1) Pak Tam"
            "name_zh": t["name_zh"],
            "area": assign_area(line, track["start"], regions),
            "place_note": PLACE_NOTES.get(t["id"]),
            "km": round(distance, 1),
            "hours": t["duration_hours"],
            "climb": int(round(ascent, -1)),
            "effort": round(effort, 1),
            "rating": rating,
            "out_and_back": out_and_back,
            "loop": loop,
            "start": [round(track["start"][0], 5), round(track["start"][1], 5)],
            "end": [round(track["end"][0], 5), round(track["end"][1], 5)],
            "route": [[round(y, 5), round(x, 5)] for x, y in simple.coords],
            "elev": profile(pts),
            "get_there": t["transport_start"] or t["transport_text"],
            "steps": split_steps(t["transport_start"] or t["transport_text"]),
            "finish": tidy_finish(t["transport_end"]),
            "finish_steps": split_steps(t["transport_end"], leaving=True),
            "description": t["description"],
            "photo": (t["gallery"][0]["url"] if t["gallery"] else t["thumbnail"]),
            "source_url": t["source_url"],
        })
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "trails.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_region_paths(regions)
    summary = {}
    for t in out:
        summary.setdefault(t["area"], {}).setdefault(t["rating"], 0)
        summary[t["area"]][t["rating"]] += 1
    print(f"Wrote {len(out)} trails ({(OUT / 'trails.json').stat().st_size // 1024} KB)")
    for area, counts in sorted(summary.items()):
        print(f"  {area:10} {counts}")


def write_region_paths(regions):
    """Project the six areas to a 1000-wide SVG for the 'Starting where?' map."""
    allb = [g.bounds for g in regions.values()]
    minx, miny = min(b[0] for b in allb), min(b[1] for b in allb)
    maxx, maxy = max(b[2] for b in allb), max(b[3] for b in allb)
    k = math.cos(math.radians((miny + maxy) / 2))
    sc = 1000 / ((maxx - minx) * k)
    height = round((maxy - miny) * sc)
    paths = {}
    for key, g in regions.items():
        segs = []
        for p in parts(g):
            if p.area < 2e-5:
                continue
            p = p.simplify(0.0012, preserve_topology=True)
            ring = [((x - minx) * k * sc, (maxy - y) * sc) for x, y in p.exterior.coords][:-1]
            if len(ring) >= 3:
                segs.append("M" + " L".join(f"{x:.0f} {y:.0f}" for x, y in ring) + " Z")
        paths[key] = " ".join(segs)
    (OUT / "regions.json").write_text(json.dumps({"viewBox": f"0 0 1000 {height}", "paths": paths}), encoding="utf-8")


if __name__ == "__main__":
    build()
