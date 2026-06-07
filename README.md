# India Tech Company Finder

A Python CLI that automatically collects **tech-company candidates across major Indian tech hubs** and exports them to CSV + JSON.

It combines:

- **OpenStreetMap / Overpass** — free, no API key, lower coverage.
- **Google Places API** — optional API key, much better coverage, uses official APIs instead of scraping Google Maps.
- **CSV seed import** — optional, for your own lists that should be de-duplicated with API results.

> No public source can guarantee a perfect list of *all* companies. This project now uses curated tech-zone searches plus granular city-grid searches and rotating batches to improve recall without hammering APIs. Treat the output as a ranked candidate list and verify important rows manually.

## Built-in coverage

Default preset: `india-tech-cities`.

It searches these tech hubs:

```text
bengaluru, hyderabad, pune, mumbai, navi-mumbai, chennai,
gurugram, noida, greater-noida, new-delhi, ghaziabad, faridabad,
kolkata, ahmedabad, gandhinagar, surat, vadodara, kochi,
thiruvananthapuram, chandigarh-mohali, jaipur, indore, bhopal,
coimbatore, trichy, madurai, bhubaneswar, mysuru, mangaluru,
hubballi-dharwad, belagavi, nagpur, nashik, visakhapatnam,
vijayawada, guntur, warangal, lucknow, panaji-goa, guwahati,
patna, ranchi, raipur, dehradun
```

Other presets:

- `bengaluru` — full Bengaluru city area
- `hsr` — only HSR Layout, Bengaluru
- `india` — alias for `india-tech-cities`

## Setup

```bash
cd india-tech-company-finder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional Google Places setup:

```bash
cp .env.example .env
# edit .env and set GOOGLE_PLACES_API_KEY=your_key
```

## Run

Free OSM-only run across Indian tech cities:

```bash
python -m india_tech_finder.cli find --sources osm
```

Recommended hybrid run with Google Places enabled:

```bash
python -m india_tech_finder.cli find --sources osm,google --granularity hybrid --grid-size-m 5000
```

If you are hitting 429s, run a small rotating batch and merge it into existing results:

```bash
python -m india_tech_finder.cli find \
  --sources osm,google \
  --granularity hybrid \
  --region-batch-size 1 \
  --google-point-batch-size 6 \
  --batch-index 0 \
  --no-google-details \
  --merge-existing
```

Increase `--batch-index` on the next run to process the next slice.

Outputs:

- `results/india_tech_companies.csv`
- `results/india_tech_companies.json`

## Granularity and 429 strategy

The finder now has a curated tech-zone list in:

```text
india_tech_finder/data/tech_zones.json
```

It currently contains **283 curated tech zones across 44 Indian city regions**, including high-priority areas/parks like Whitefield, HITEC City, Hinjewadi, OMR, Cyber City, Sector 62, Salt Lake Sector V, Infopark, Technopark, and more.

The default `--granularity hybrid` mode does both:

1. Search curated tech-zone points first.
2. Then search the whole city bounding box with overlapping grid points.

So it does **not rely only on known tech zones**. Companies outside famous corridors/parks are still covered by the grid.

Because Google/Overpass can return 429 when too many requests happen in one burst, the GitHub Action uses:

- one city/region per run by default
- six Google zone/grid points per run by default
- `--merge-existing` so results accumulate over time
- `--no-google-details` by default to avoid extra Place Details requests
- retries and exponential backoff for 429 responses

For maximum recall on a one-off local run, you can increase:

```bash
python -m india_tech_finder.cli find \
  --sources osm,google \
  --granularity hybrid \
  --grid-size-m 3000 \
  --max-pages 3 \
  --google-request-sleep-s 0.5
```

That is much more expensive, so use it carefully with Google billing/quota limits.

## Useful options

```bash
# Only Bengaluru
python -m india_tech_finder.cli find --preset bengaluru --sources osm,google

# Only specific cities from the India preset
python -m india_tech_finder.cli find --region bengaluru --region hyderabad --sources osm,google

# Legacy HSR-only search
python -m india_tech_finder.cli find --preset hsr --sources osm,google

# Export more review candidates
python -m india_tech_finder.cli find --sources osm,google --min-score 0

# Search only curated tech zones
python -m india_tech_finder.cli find --granularity zones --sources osm,google

# Add your own Google query template
python -m india_tech_finder.cli find --query "software product company"

# Deeper but more expensive Google search
python -m india_tech_finder.cli find --max-pages 3 --google-request-sleep-s 0.5

# Use custom output files
python -m india_tech_finder.cli find --out-csv results/custom.csv --out-json results/custom.json

# Import and dedupe your own seed CSV
python -m india_tech_finder.cli find --sources osm,google,csv --seed-csv my_companies.csv
```

Seed CSV headers supported: `name,city,region,country,address,lat,lng,website,phone,categories`.

## Curated tech zones file

The built-in curated zones file is:

```text
india_tech_finder/data/tech_zones.json
```

Each row has:

```json
{
  "region_id": "bengaluru",
  "name": "Whitefield",
  "query": "Whitefield Bengaluru",
  "lat": 12.9698,
  "lng": 77.75,
  "radius_m": 6000
}
```

Use your own file with:

```bash
python -m india_tech_finder.cli find --granularity hybrid --tech-zones-file my_zones.json
```

## Custom regions file

You can provide your own city list as JSON:

```json
[
  {
    "id": "bengaluru-whitefield",
    "city": "Whitefield, Bengaluru",
    "state": "Karnataka",
    "query": "Whitefield Bengaluru",
    "lat": 12.9698,
    "lng": 77.7500,
    "radius_m": 8000,
    "bbox": [12.90, 77.68, 13.03, 77.82]
  }
]
```

Run:

```bash
python -m india_tech_finder.cli find --regions-file regions.json --sources osm,google
```

## GitHub Actions cron

A workflow is included at:

```text
.github/workflows/find-india-tech-companies.yml
```

It runs every 6 hours in small rotating batches and can also be started manually from **Actions → Find India tech companies → Run workflow**.

To enable Google Places in GitHub:

1. Open your GitHub repo.
2. Go to **Settings → Secrets and variables → Actions → Secrets**.
3. Add `GOOGLE_PLACES_API_KEY`.

Every run uploads `results/india_tech_companies.csv` and `.json` as an artifact, merges the new batch with existing results, and commits changed result files back to the repo automatically.

Optional repo variables under **Settings → Secrets and variables → Actions → Variables**:

- `TECH_FINDER_PRESET` — default `india-tech-cities`
- `TECH_FINDER_REGIONS` — comma-separated subset, e.g. `bengaluru,hyderabad,pune`
- `TECH_FINDER_SOURCES` — default `osm,google`
- `TECH_FINDER_GRANULARITY` — default `hybrid`; choices: `city`, `zones`, `grid`, `hybrid`
- `TECH_FINDER_GRID_SIZE_M` — default `5000`
- `TECH_FINDER_GRID_RADIUS_M` — optional; default equals grid size
- `TECH_FINDER_ZONE_RADIUS_M` — default `4000`
- `TECH_FINDER_REGION_BATCH_SIZE` — default `1`, to avoid Overpass/Google bursts
- `TECH_FINDER_GOOGLE_POINT_BATCH_SIZE` — default `6`, number of grid points per run
- `TECH_FINDER_BATCH_INDEX` — optional; defaults to GitHub run number for rotation
- `TECH_FINDER_MIN_SCORE` — default `20`
- `TECH_FINDER_MAX_PAGES` — default `1`; use `3` for deeper Google results, but it costs more
- `TECH_FINDER_NO_GOOGLE_DETAILS` — default `true` in cron to reduce Google API calls by skipping Place Details
- `TECH_FINDER_GOOGLE_REQUEST_SLEEP_S` — default `0.3`, small pause between Google API calls

## How scoring works

Each candidate gets a `tech_score` from 0-100 based on keywords such as `software`, `technology`, `SaaS`, `cloud`, `data analytics`, `app development`, OSM office tags, Google query matches, website/phone presence, and negative hints like `hotel`, `restaurant`, `training institute`, etc.

Use `city`, `region`, `confidence`, and `notes` columns to decide what to verify.
