# HSR Tech Company Finder

A small Python CLI that automatically collects **tech-company candidates in/around HSR Layout, Bengaluru** and exports them to CSV + JSON.

It combines:

- **OpenStreetMap / Overpass** — free, no API key, lower coverage.
- **Google Places API** — optional API key, much better coverage, uses official APIs instead of scraping Google Maps.
- **CSV seed import** — optional, for your own lists that should be de-duplicated with the API results.

> No public source can guarantee a perfect list of *all* companies. Treat the output as a ranked candidate list and verify important rows manually.

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

Free OSM-only run:

```bash
python -m hsr_tech_finder.cli find --sources osm
```

Recommended run with Google Places enabled:

```bash
python -m hsr_tech_finder.cli find --sources osm,google
```

Outputs:

- `results/hsr_tech_companies.csv`
- `results/hsr_tech_companies.json`

## GitHub Actions cron

A workflow is included at:

```text
.github/workflows/find-hsr-tech-companies.yml
```

It runs daily at `03:30 UTC` and can also be started manually from **Actions → Find HSR tech companies → Run workflow**.

To enable Google Places in GitHub:

1. Open your GitHub repo.
2. Go to **Settings → Secrets and variables → Actions → Secrets**.
3. Add `GOOGLE_PLACES_API_KEY`.

Every run uploads `results/hsr_tech_companies.csv` and `.json` as an artifact, and commits changed result files back to the repo automatically.

Optional repo variables under **Settings → Secrets and variables → Actions → Variables**:

- `HSR_SOURCES` — default `osm,google`
- `HSR_MIN_SCORE` — default `20`
- `HSR_MAX_PAGES` — default `3`

## Useful options

```bash
# Export more review candidates
python -m hsr_tech_finder.cli find --sources osm,google --min-score 0

# Add your own Google query
python -m hsr_tech_finder.cli find --query "software development company HSR Layout Bengaluru"

# Use a custom output file
python -m hsr_tech_finder.cli find --out-csv results/custom.csv

# Import and dedupe your own seed CSV
python -m hsr_tech_finder.cli find --sources osm,google,csv --seed-csv my_companies.csv
```

Seed CSV headers supported: `name,address,lat,lng,website,phone,categories`.

## How scoring works

Each candidate gets a `tech_score` from 0-100 based on keywords such as `software`, `technology`, `SaaS`, `cloud`, `data analytics`, `app development`, OSM office tags, Google query matches, website/phone presence, and negative hints like `hotel`, `restaurant`, `training institute`, etc.

Use `confidence` and `notes` columns to decide what to verify.
