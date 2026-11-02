# take a hike 🥾

A Hong Kong hiking site: browse trails, get a random hike for the day, tick off
sections of the big four trails (MacLehose, Wilson, Hong Kong, Lantau), and
eventually plan walking routes from A to B.

## Layout

```
scraper/   Python scripts that collect trail data
data/      Output of the scrapers (the site reads from here)
  trails.json   full trail records
  trails.csv    flat summary, handy for spreadsheets
  gpx/          GPS tracks per trail
  raw/          cached HTML (git-ignored)
web/       The website (static HTML/CSS/JS for now)
archive/   Old code and data kept for reference (git-ignored); safe to delete later
```

Live at https://jasonyin.com/takeahike/

## Updating the trail data

```
pip install -r scraper/requirements.txt
python scraper/scrape_hiking_gov.py            # uses cached pages
python scraper/scrape_hiking_gov.py --refresh  # re-download from hiking.gov.hk
```

## Running the site locally

The page loads its data with `fetch`, which browsers block for `file://`
pages, so serve the `web` folder:

```
python -m http.server 8000 --directory web
```

then open http://localhost:8000/

## Rebuilding the site's data

After re-scraping, regenerate the compact file the site loads
(areas, our difficulty rating, simplified routes, elevation profiles):

```
pip install shapely
python scraper/build_web_data.py
```

`data/geo/hk_regions.geojson` (the six areas) comes from
`scraper/make_regions.py`; it only needs rebuilding if district boundaries change.

Maps use the Lands Department's free basemap tiles (mapapi.geodata.gov.hk),
credited on screen as "Map © Lands Department, HKSAR".

## Difficulty ratings

Worked out from GPS data, never from opinions:

- **Brutal** when `√(km × metres climbed)` is 75 or more, so it takes both
  length and climbing: 10 km with 600 m is Brutal, 15 km with 250 m is not.
- Otherwise by effort km (distance + 1 km per 100 m climbed): under 5 is **Easy**,
  under 10 is **Moderate**, the rest are **Hard**.

## Publishing

GitHub Pages serves the `gh-pages` branch, which holds just the `web` folder.
Commit your changes, then:

```
git subtree split --prefix web -b gh-pages
git push origin gh-pages
```

The site updates a minute or so later.

## Data sources

- [hiking.gov.hk](https://www.hiking.gov.hk/trail) (AFCD): trail attributes,
  transport info, photos, GPX tracks.
- Planned: [DATA.GOV.HK "Hiking Trails in Country Parks"](https://data.gov.hk/en-data/dataset/hk-afcd-afcdlist-hikingtrailscp)
  open data, and OpenStreetMap for routing.
