# India Tech Company Finder

A Python CLI that automatically collects **tech-company candidates across major Indian tech hubs** and exports them to CSV + JSON.

It combines:

- **OpenStreetMap / Overpass** — free, no API key, lower coverage.
- **Google Places API** — optional API key, much better coverage, uses official APIs instead of scraping Google Maps.
- **CSV seed import** — optional, for your own lists that should be de-duplicated with API results.

> No public source can guarantee a perfect list of *all* companies. Treat the output as a ranked candidate list and verify important rows manually.

## Built-in coverage

Default preset: `india-tech-cities`.

It searches these tech hubs:

```text
bengaluru, hyderabad, pune, mumbai, navi-mumbai, chennai,
gurugram, noida, new-delhi, kolkata, ahmedabad, gandhinagar,
kochi, thiruvananthapuram, chandigarh-mohali, jaipur, indore,
coimbatore, bhubaneswar, mysuru, mangaluru, nagpur,
visakhapatnam, lucknow
```

Other presets:

- `bengaluru` — full Bengaluru city area
- `hsr` — only HSR Layout, Bengaluru
- `india` — alias for `india-tech-cities`

## Setup

```bash
cd hsr-tech-company-finder
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
python -m hsr_tech_finder.cli find --sources osm
```

Recommended run with Google Places enabled:

```bash
python -m hsr_tech_finder.cli find --sources osm,google
```

Outputs:

- `results/india_tech_companies.csv`
- `results/india_tech_companies.json`

## Useful options

```bash
# Only Bengaluru
python -m hsr_tech_finder.cli find --preset bengaluru --sources osm,google

# Only specific cities from the India preset
python -m hsr_tech_finder.cli find --region bengaluru --region hyderabad --sources osm,google

# Legacy HSR-only search
python -m hsr_tech_finder.cli find --preset hsr --sources osm,google

# Export more review candidates
python -m hsr_tech_finder.cli find --sources osm,google --min-score 0

# Add your own Google query template
python -m hsr_tech_finder.cli find --query "software product company {city}"

# Use custom output files
python -m hsr_tech_finder.cli find --out-csv results/custom.csv --out-json results/custom.json

# Import and dedupe your own seed CSV
python -m hsr_tech_finder.cli find --sources osm,google,csv --seed-csv my_companies.csv
```

Seed CSV headers supported: `name,city,region,country,address,lat,lng,website,phone,categories`.

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
python -m hsr_tech_finder.cli find --regions-file regions.json --sources osm,google
```

## GitHub Actions cron

A workflow is included at:

```text
.github/workflows/find-india-tech-companies.yml
```

It runs daily at `03:30 UTC` and can also be started manually from **Actions → Find India tech companies → Run workflow**.

To enable Google Places in GitHub:

1. Open your GitHub repo.
2. Go to **Settings → Secrets and variables → Actions → Secrets**.
3. Add `GOOGLE_PLACES_API_KEY`.

Every run uploads `results/india_tech_companies.csv` and `.json` as an artifact, and commits changed result files back to the repo automatically.

Optional repo variables under **Settings → Secrets and variables → Actions → Variables**:

- `TECH_FINDER_PRESET` — default `india-tech-cities`
- `TECH_FINDER_REGIONS` — comma-separated subset, e.g. `bengaluru,hyderabad,pune`
- `TECH_FINDER_SOURCES` — default `osm,google`
- `TECH_FINDER_MIN_SCORE` — default `20`
- `TECH_FINDER_MAX_PAGES` — default `1`; use `3` for deeper Google results, but it costs more
- `TECH_FINDER_NO_GOOGLE_DETAILS` — set to `true` to reduce Google API calls by skipping Place Details

## How scoring works

Each candidate gets a `tech_score` from 0-100 based on keywords such as `software`, `technology`, `SaaS`, `cloud`, `data analytics`, `app development`, OSM office tags, Google query matches, website/phone presence, and negative hints like `hotel`, `restaurant`, `training institute`, etc.

Use `city`, `region`, `confidence`, and `notes` columns to decide what to verify.
