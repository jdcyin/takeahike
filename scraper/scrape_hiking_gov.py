"""Scrape every trail from https://www.hiking.gov.hk/trail into data/.

Outputs
  data/trails.json   full records (one object per trail)
  data/trails.csv    flat summary for spreadsheets
  data/gpx/*.gpx     GPS tracks, one or more per trail

Usage
  python scraper/scrape_hiking_gov.py            # uses cached pages in data/raw/
  python scraper/scrape_hiking_gov.py --refresh  # re-download everything

Pages are cached in data/raw/ (git-ignored) so re-running while developing
the parser doesn't hit the government site again.
"""

import argparse
import csv
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.hiking.gov.hk"
LIST_URL = BASE + "/trail"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
GPX_DIR = DATA / "gpx"
DELAY_SECONDS = 1.0  # be polite: one request per second
USER_AGENT = "takeahike-scraper/0.1 (personal hiking project)"


# ---------------------------------------------------------------- fetching

class Fetcher:
    """Session pinned to one site language, with an on-disk page cache."""

    def __init__(self, lang, refresh):
        self.lang = lang  # "en-us" or "zh-tw"
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._lang_set = False

    def _set_language(self):
        # The site stores language in the session: GET a page for the CSRF
        # tokens, then POST to /api/switch_lang (what the "Eng" link does).
        page = self.session.get(LIST_URL, timeout=60)
        soup = BeautifulSoup(page.text, "html.parser")
        token = soup.select_one('meta[name="token"]')
        csrf = soup.select_one('meta[name="csrf-token"]')
        self.session.post(
            BASE + "/api/switch_lang",
            data={"id": self.lang, "csrf_token": token["content"] if token else ""},
            headers={"X-CSRF-TOKEN": csrf["content"] if csrf else ""},
            timeout=60,
        ).raise_for_status()
        self._lang_set = True

    def get(self, url, cache_name, binary=False):
        path = RAW / self.lang / cache_name
        if path.exists() and not self.refresh:
            return path.read_bytes() if binary else path.read_text(encoding="utf-8")
        if not self._lang_set:
            self._set_language()
        time.sleep(DELAY_SECONDS)
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(resp.content)
            return resp.content
        resp.encoding = "utf-8"
        path.write_text(resp.text, encoding="utf-8")
        return resp.text


# ---------------------------------------------------------------- helpers

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def gov_key_from_url(url):
    return url.rstrip("/").split("/id/")[-1]


def parse_duration_hours(text):
    """'1 hour' -> 1.0, '2.5 hours' -> 2.5, '1 hour 30 minutes' -> 1.5"""
    text = text.lower()
    hours = re.search(r"([\d.]+)\s*(?:hour|hr)", text)
    mins = re.search(r"([\d.]+)\s*(?:minute|min)", text)
    if not hours and not mins:
        return None
    total = float(hours.group(1)) if hours else 0.0
    if mins:
        total += float(mins.group(1)) / 60
    return round(total, 2)


def parse_km(text):
    m = re.search(r"([\d.]+)\s*km", text.lower())
    return float(m.group(1)) if m else None


def parse_rating(text):
    """'Overall Difficulty (1-Star Easy)' -> (1, 'Easy'); '(Rating 2-Heart)' -> (2, None)"""
    m = re.search(r"\((?:Rating\s*)?(\d)\s*-\s*(?:Star|Heart)\s*([A-Za-z ]*)\)", text)
    if not m:
        return None, None
    return int(m.group(1)), clean(m.group(2)) or None


def parse_section(name):
    """'Lantau Trail (Section 1) Mui Wo to Nam Shan' -> (1, 'Mui Wo', 'Nam Shan')"""
    m = re.search(r"\(Section\s*(\d+)\)\s*(.*)", name)
    if not m:
        return None, None, None
    number, rest = int(m.group(1)), clean(m.group(2))
    ends = re.split(r"\s+to\s+", rest, maxsplit=1)
    if len(ends) == 2:
        return number, ends[0], ends[1]
    return number, None, None


# ---------------------------------------------------------------- listing page

def parse_filter_labels(soup):
    """Map CSS filter classes (e.g. 'type_field_4') to their visible labels."""
    labels = {}
    for el in soup.select(".criteria-item[data-id]"):
        key = el["data-id"]
        text = clean(el.get_text())
        if not text:
            label = soup.select_one(f'label[for="{key}"]')
            text = clean(label.get_text()) if label else ""
        if text and not text.startswith("web."):
            labels[key] = text
    return labels


def parse_listing(html):
    """Return {gov_key: card_info}. The page renders each trail twice
    (grid + list layouts), so cards are de-duplicated by gov_key."""
    soup = BeautifulSoup(html, "html.parser")
    labels = parse_filter_labels(soup)
    cards = {}
    for item in soup.select(".trail-item"):
        link = item.select_one("[data-url]") or item.select_one('a[href*="/trail/info/id/"]')
        if not link:
            continue
        url = link.get("data-url") or link.get("href")
        key = gov_key_from_url(url)
        classes = item.get("class", [])

        def field(prefix):
            for c in classes:
                if c.startswith(prefix + "_field_"):
                    return c
            return None

        title = item.select_one("h1")
        region = item.select_one("p.region")
        thumb = item.select_one("[data-bg]")
        gov_id = next((int(c[6:]) for c in classes if re.fullmatch(r"trail_\d+", c)), None)
        card = cards.setdefault(key, {"gov_key": key, "url": url})
        # Fill any gaps: the two layouts don't show identical fields.
        card.setdefault("gov_id", gov_id)
        if title and not card.get("name"):
            card["name"] = clean(title.get_text())
        if region and not card.get("region"):
            card["region"] = clean(region.get_text())
        if thumb and not card.get("thumbnail"):
            card["thumbnail"] = thumb["data-bg"]
        for prefix in ("type", "shading"):
            cls = field(prefix)
            if cls and not card.get(prefix):
                card[prefix] = labels.get(cls, cls)
    return cards


# ---------------------------------------------------------------- detail page

def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    h1 = soup.find("h1", class_="mt-4")  # trail title: "<name><br><small>parent</small>"
    if h1:
        small = h1.find("small")
        out["parent_trail"] = clean(small.get_text()) if small else None
        if small:
            small.extract()
        out["name"] = clean(h1.get_text())

    attrs = soup.select_one("section.trail-section .trail-rating")
    ratings = {}
    if attrs:
        for row in attrs.select(".row"):
            label_el = row.select_one(".col-4")
            if not label_el:
                continue
            label = clean(label_el.get_text())
            value_el = row.select_one(".col-8")
            value = clean(value_el.get_text()) if value_el else ""
            lower = label.lower()
            if lower == "length":
                out["length_km"] = parse_km(value)
                out["length_text"] = value
            elif lower == "duration":
                out["duration_hours"] = parse_duration_hours(value)
                out["duration_text"] = value
            elif lower == "region":
                out["region"] = value
            elif lower.startswith("overall difficulty"):
                out["difficulty"], out["difficulty_label"] = parse_rating(label)
            elif lower.startswith("length"):
                ratings["length"] = parse_rating(label)[0]
            elif lower.startswith("duration"):
                ratings["duration"] = parse_rating(label)[0]
            elif lower.startswith("total elevation gain"):
                ratings["elevation_gain"] = parse_rating(label)[0]
            elif lower.startswith("surface"):
                ratings["surface"] = parse_rating(label)[0]
        # Shading and popularity sit outside the .row pairs; read them from text.
        text = clean(attrs.get_text(" "))
        shade = re.search(r"Shading Level.*?\((High|Medium|Low)\)", text)
        ratings["shading"] = shade.group(1) if shade else None
        pop = re.search(r"Popularity Rating.*?\(Rating (\d)-Heart\)", text)
        ratings["popularity"] = int(pop.group(1)) if pop else None
    out["ratings"] = ratings

    desc = soup.select_one("section.trail-section .col-lg-8")
    out["description"] = clean(desc.get_text(" ")) if desc else None

    transport = soup.select_one("#trail-transportation .trail-rating")
    if transport:
        # Text is spread over several <p>/<table> children; skip the photo
        # carousel and the "for reference only" disclaimer (div.mt-5).
        parts = [
            el.get_text(" ")
            for el in transport.find_all(recursive=False)
            if not set(el.get("class") or []) & {"swiper-container", "mt-5"}
        ]
        raw = clean(" ".join(parts))
        out["transport_text"] = raw
        # Pages use both "Starting Point/Endpoint" and "Start Point/End Point".
        # Loop walks often have no endpoint at all.
        start = re.search(r"Start(?:ing)? Point\s*(.*?)(?=End ?Point|$)", raw, re.I)
        end = re.search(r"End ?Point\s*(.*)", raw, re.I)
        out["transport_start"] = clean(start.group(1)) if start else None
        out["transport_end"] = clean(end.group(1)) if end else None
        out["transport_photos"] = [
            {"caption": clean(a.get("title")), "url": a.get("href")}
            for a in transport.select("a.fancybox-button")
        ]

    out["gallery"] = [
        {"caption": clean(a.get("title")), "url": a.get("href")}
        for a in soup.select("#gallery a.grid-item")
    ]

    links = {}
    for block in soup.select("section#trail-map .other-trail"):
        heading = block.find("small")
        name = clean(heading.get_text()).lower() if heading else ""
        links[name] = [
            {"name": clean(a.get_text()), "url": a.get("href")}
            for a in block.select("a[href]")
        ]
    out["related_trails"] = links.get("related trails", [])
    out["nearby_attractions"] = links.get("nearby attraction", [])

    geo = soup.select_one('a[href*="map.gov.hk"]')
    out["geoinfo_map_url"] = geo["href"] if geo else None

    out["gpx_urls"] = sorted(set(re.findall(r"/trail/get_gpx/id/[A-Za-z0-9=%]+/key/\d+", html)))
    return out


# ---------------------------------------------------------------- GPX stats

def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def gpx_points(data):
    root = ET.fromstring(data)
    pts = []
    for el in root.iter():
        if el.tag.split("}")[-1] in ("trkpt", "rtept"):
            ele = next((c.text for c in el if c.tag.split("}")[-1] == "ele"), None)
            pts.append((float(el.get("lat")), float(el.get("lon")), float(ele) if ele else None))
    return pts


def median_smooth(values, window=7):
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = sorted(values[max(0, i - half): i + half + 1])
        out.append(chunk[len(chunk) // 2])
    return out


def track_stats(pts, climb_threshold_m=5.0):
    """Distance plus ascent/descent. Elevations are median-smoothed to drop
    single-point spikes, then changes under the threshold are ignored so
    GPS noise doesn't inflate the climb."""
    if len(pts) < 2:
        return None
    dist = sum(haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    eles = [p[2] for p in pts if p[2] is not None]
    ascent = descent = 0.0
    if eles:
        smooth = median_smooth(eles)
        ref = smooth[0]
        for e in smooth[1:]:
            if e - ref >= climb_threshold_m:
                ascent += e - ref
                ref = e
            elif ref - e >= climb_threshold_m:
                descent += ref - e
                ref = e
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    return {
        "distance_km": round(dist, 2),
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "max_ele_m": max(eles) if eles else None,
        "min_ele_m": min(eles) if eles else None,
        "start": [pts[0][0], pts[0][1]],
        "end": [pts[-1][0], pts[-1][1]],
        "bbox": [min(lats), min(lons), max(lats), max(lons)],
        "points": len(pts),
    }


# ---------------------------------------------------------------- main

def scrape(refresh):
    en = Fetcher("en-us", refresh)
    zh = Fetcher("zh-tw", refresh)

    cards = parse_listing(en.get(LIST_URL, "list.html"))
    zh_cards = parse_listing(zh.get(LIST_URL, "list.html"))
    print(f"Found {len(cards)} unique trails")

    GPX_DIR.mkdir(parents=True, exist_ok=True)
    trails, used_slugs = [], set()
    for i, (key, card) in enumerate(sorted(cards.items(), key=lambda kv: kv[1].get("name", "")), 1):
        print(f"[{i}/{len(cards)}] {card.get('name')}")
        try:
            detail = parse_detail(en.get(card["url"], f"trail_{key}.html"))
        except requests.RequestException as e:
            print(f"   ! failed: {e}")
            continue

        name = detail.get("name") or card.get("name")
        slug = slugify(name)
        while slug in used_slugs:
            slug += "-x"
        used_slugs.add(slug)

        section, section_from, section_to = parse_section(name)
        trail = {
            "id": slug,
            "name_en": name,
            "name_zh": zh_cards.get(key, {}).get("name"),
            "parent_trail": detail.get("parent_trail"),
            "section": section,
            "section_from": section_from,
            "section_to": section_to,
            "region": detail.get("region") or card.get("region"),
            "type": card.get("type"),
            "length_km": detail.get("length_km"),
            "duration_hours": detail.get("duration_hours"),
            "difficulty": detail.get("difficulty"),
            "difficulty_label": detail.get("difficulty_label"),
            "ratings": detail.get("ratings", {}),
            "description": detail.get("description"),
            "transport_start": detail.get("transport_start"),
            "transport_end": detail.get("transport_end"),
            "transport_text": detail.get("transport_text"),
            "transport_photos": detail.get("transport_photos", []),
            "gallery": detail.get("gallery", []),
            "thumbnail": card.get("thumbnail"),
            "related_trails": detail.get("related_trails", []),
            "nearby_attractions": detail.get("nearby_attractions", []),
            "geoinfo_map_url": detail.get("geoinfo_map_url"),
            "source_url": card["url"],
            "gov_id": card.get("gov_id"),
            "gov_key": key,
            "gpx_files": [],
            "track": None,
        }

        all_pts = []
        for n, gpx_path in enumerate(detail.get("gpx_urls", [])):
            try:
                data = en.get(BASE + gpx_path, f"gpx_{key}_{n}.gpx", binary=True)
                pts = gpx_points(data)
            except (requests.RequestException, ET.ParseError) as e:
                print(f"   ! gpx {n} failed: {e}")
                continue
            if not pts:
                continue
            out_name = f"{slug}.gpx" if n == 0 else f"{slug}-{n}.gpx"
            (GPX_DIR / out_name).write_bytes(data)
            trail["gpx_files"].append(f"gpx/{out_name}")
            all_pts.extend(pts)
        trail["track"] = track_stats(all_pts)
        trails.append(trail)

    return trails


def write_outputs(trails):
    meta = {
        "source": LIST_URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(trails),
    }
    (DATA / "trails.json").write_text(
        json.dumps({"meta": meta, "trails": trails}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    columns = [
        "id", "name_en", "name_zh", "parent_trail", "section", "region", "type",
        "length_km", "duration_hours", "difficulty", "difficulty_label",
        "track_distance_km", "track_ascent_m", "start_lat", "start_lon",
        "end_lat", "end_lon", "transport_start", "transport_end", "source_url",
    ]
    with open(DATA / "trails.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for t in trails:
            tr = t["track"] or {}
            row = {c: t.get(c) for c in columns}
            row.update(
                track_distance_km=tr.get("distance_km"),
                track_ascent_m=tr.get("ascent_m"),
                start_lat=(tr.get("start") or [None, None])[0],
                start_lon=(tr.get("start") or [None, None])[1],
                end_lat=(tr.get("end") or [None, None])[0],
                end_lon=(tr.get("end") or [None, None])[1],
            )
            w.writerow(row)
    print(f"Wrote {len(trails)} trails to {DATA / 'trails.json'} and trails.csv")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="ignore the page cache and re-download")
    args = parser.parse_args()
    write_outputs(scrape(args.refresh))
