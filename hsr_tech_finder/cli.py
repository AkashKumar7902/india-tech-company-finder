from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from .dedupe import dedupe_companies
from .export import write_csv, write_json
from .models import Company
from .regions import PRESETS, Region, filter_regions, load_regions_file
from .scoring import score_company
from .sources.csv_seed import fetch_csv_seed
from .sources.google_places import DEFAULT_QUERY_TEMPLATES, fetch_google_places, render_queries
from .sources.osm import OVERPASS_URL, fetch_osm


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_sources(text: str) -> set[str]:
    aliases = {
        "google": "google_places",
        "places": "google_places",
        "google_places": "google_places",
        "osm": "openstreetmap",
        "openstreetmap": "openstreetmap",
        "csv": "seed_csv",
        "seed": "seed_csv",
        "seed_csv": "seed_csv",
    }
    sources = set()
    for part in text.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part not in aliases:
            raise argparse.ArgumentTypeError(f"unknown source '{part}'")
        sources.add(aliases[part])
    return sources


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    try:
        parts = [float(value.strip()) for value in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox must be: south,west,north,east") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must contain 4 numbers: south,west,north,east")
    south, west, north, east = parts
    if south >= north or west >= east:
        raise argparse.ArgumentTypeError("bbox must satisfy south<north and west<east")
    return south, west, north, east


def bbox_from_center(lat: float, lng: float, radius_m: int) -> tuple[float, float, float, float]:
    lat_delta = radius_m / 111_000
    lng_delta = radius_m / (111_000 * max(math.cos(math.radians(lat)), 0.1))
    return lat - lat_delta, lng - lng_delta, lat + lat_delta, lng + lng_delta


def load_query_templates(args: argparse.Namespace) -> list[str]:
    queries: list[str] = []
    if args.queries_file:
        for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(line)
    queries.extend(args.query or [])
    return queries or list(DEFAULT_QUERY_TEMPLATES)


def resolve_regions(args: argparse.Namespace) -> list[Region]:
    if args.lat is not None or args.lng is not None or args.bbox is not None:
        if args.lat is None or args.lng is None:
            raise SystemExit("Custom location requires both --lat and --lng.")
        radius_m = args.radius_m or 20_000
        bbox = args.bbox or bbox_from_center(args.lat, args.lng, radius_m)
        return [
            Region(
                id="custom",
                city=args.city_name,
                state=args.state_name,
                query=args.city_query or args.city_name,
                lat=args.lat,
                lng=args.lng,
                radius_m=radius_m,
                bbox=bbox,
            )
        ]

    if args.regions_file:
        regions = load_regions_file(args.regions_file)
    else:
        regions = list(PRESETS[args.preset])

    regions = filter_regions(regions, args.region)
    if args.radius_m is not None:
        regions = [region.with_radius(args.radius_m) for region in regions]
    if not regions:
        raise SystemExit("No regions selected.")
    return regions


def _safe_fetch(
    label: str,
    fetcher: Callable[[], list[Company]],
    *,
    strict: bool,
) -> list[Company]:
    print(f"Fetching {label}...")
    try:
        companies = fetcher()
    except Exception as exc:  # noqa: BLE001 - CLI should continue by default.
        if strict:
            raise
        print(f"Warning: {label} failed: {exc}", file=sys.stderr)
        return []
    print(f"  {label}: {len(companies)} candidates")
    return companies


def cmd_find(args: argparse.Namespace) -> int:
    load_dotenv(args.env)
    requested_sources = parse_sources(args.sources)
    regions = resolve_regions(args)
    all_companies: list[Company] = []
    source_counts: dict[str, int] = {source: 0 for source in requested_sources}

    google_api_key = None
    if "google_places" in requested_sources:
        google_api_key = args.google_api_key or os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
        if not google_api_key:
            print(
                "Skipping Google Places: set GOOGLE_PLACES_API_KEY in .env or pass --google-api-key.",
                file=sys.stderr,
            )
            source_counts["google_places"] = 0

    query_templates = load_query_templates(args)

    print(f"Selected {len(regions)} region(s): {', '.join(region.label for region in regions)}")

    for region in regions:
        print(f"\n== {region.label} ==")
        if "openstreetmap" in requested_sources:
            companies = _safe_fetch(
                f"OpenStreetMap/Overpass ({region.label})",
                lambda region=region: fetch_osm(
                    region.bbox,
                    city=region.city,
                    region=region.state,
                    country=region.country,
                    overpass_url=args.overpass_url,
                    timeout=args.timeout,
                ),
                strict=args.strict,
            )
            source_counts["openstreetmap"] = source_counts.get("openstreetmap", 0) + len(companies)
            all_companies.extend(companies)

        if "google_places" in requested_sources and google_api_key:
            queries = render_queries(
                query_templates,
                city=region.query,
                state=region.state,
                country=region.country,
            )
            companies = _safe_fetch(
                f"Google Places ({region.label})",
                lambda region=region, queries=queries: fetch_google_places(
                    google_api_key,
                    queries=queries,
                    lat=region.lat,
                    lng=region.lng,
                    city=region.city,
                    region=region.state,
                    country=region.country,
                    radius_m=region.radius_m,
                    max_pages=args.max_pages,
                    include_details=not args.no_google_details,
                    include_closed=args.include_closed,
                    timeout=args.timeout,
                ),
                strict=args.strict,
            )
            source_counts["google_places"] = source_counts.get("google_places", 0) + len(companies)
            all_companies.extend(companies)

    if "seed_csv" in requested_sources:
        if not args.seed_csv:
            print("Skipping seed CSV: pass --seed-csv path/to/file.csv.", file=sys.stderr)
            source_counts["seed_csv"] = 0
        else:
            companies = _safe_fetch(
                "seed CSV",
                lambda: fetch_csv_seed(args.seed_csv),
                strict=args.strict,
            )
            source_counts["seed_csv"] = len(companies)
            all_companies.extend(companies)

    deduped = dedupe_companies(all_companies)
    scored = [score_company(company) for company in deduped]
    filtered = [company for company in scored if company.tech_score >= args.min_score]
    filtered.sort(key=lambda company: (-company.tech_score, company.city.lower(), company.name.lower()))
    if args.limit:
        filtered = filtered[: args.limit]

    out_csv = args.out_csv or os.getenv("OUTPUT_CSV") or "results/india_tech_companies.csv"
    out_json = args.out_json or os.getenv("OUTPUT_JSON") or "results/india_tech_companies.json"
    csv_path = write_csv(filtered, out_csv)
    json_path = write_json(filtered, out_json)

    print("\nDone")
    print(f"  Raw candidates: {len(all_companies)} ({source_counts})")
    print(f"  After de-dupe:  {len(deduped)}")
    print(f"  Kept >= score {args.min_score}: {len(filtered)}")
    print(f"  CSV:  {csv_path}")
    print(f"  JSON: {json_path}")
    if filtered[:5]:
        print("\nTop results:")
        for company in filtered[:5]:
            location = f" ({company.city})" if company.city else ""
            print(f"  {company.tech_score:>5.1f} {company.confidence:<6} {company.name}{location}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="india-tech-finder",
        description="Find tech-company candidates across Indian tech hubs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    find = subparsers.add_parser("find", help="collect and export company candidates")
    find.add_argument("--sources", default="osm,google", help="comma-separated: osm,google,csv")
    find.add_argument("--env", default=".env", help="dotenv file containing GOOGLE_PLACES_API_KEY")
    find.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="india-tech-cities",
        help="built-in search area preset",
    )
    find.add_argument(
        "--region",
        action="append",
        help="limit preset to a region id/name, e.g. bengaluru or hyderabad; can be repeated",
    )
    find.add_argument("--regions-file", help="custom JSON array of region definitions")
    find.add_argument("--city-name", default="Custom region", help="custom region city label for --lat/--lng")
    find.add_argument("--state-name", default="", help="custom region state label for --lat/--lng")
    find.add_argument("--city-query", default="", help="custom Google query location text for --lat/--lng")
    find.add_argument("--lat", type=float, help="custom search center latitude")
    find.add_argument("--lng", type=float, help="custom search center longitude")
    find.add_argument("--radius-m", type=int, help="override Google Places search radius in metres")
    find.add_argument("--bbox", type=parse_bbox, help="custom OSM bbox as south,west,north,east")
    find.add_argument("--overpass-url", default=OVERPASS_URL, help="Overpass API endpoint")
    find.add_argument("--google-api-key", default=None, help="Google Places API key")
    find.add_argument(
        "--query",
        action="append",
        help="extra Google Places query/template; use {city}, {state}, {country}; can be repeated",
    )
    find.add_argument("--queries-file", help="file with one Google Places query/template per line")
    find.add_argument("--max-pages", type=int, default=1, help="Google pages per query; max useful value is 3")
    find.add_argument("--no-google-details", action="store_true", help="skip Place Details calls")
    find.add_argument("--include-closed", action="store_true", help="include permanently closed Google places")
    find.add_argument("--seed-csv", help="optional CSV seed file with extra candidates")
    find.add_argument("--min-score", type=float, default=20, help="minimum tech-likelihood score to export")
    find.add_argument("--limit", type=int, help="limit exported rows after scoring")
    find.add_argument("--timeout", type=int, default=60, help="network timeout seconds")
    find.add_argument("--strict", action="store_true", help="fail if a source errors instead of warning")
    find.add_argument("--out-csv", default=None, help="CSV output path")
    find.add_argument("--out-json", default=None, help="JSON output path")
    find.set_defaults(func=cmd_find)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
