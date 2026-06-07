from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from .dedupe import dedupe_companies
from .export import write_csv, write_json
from .geo import HSR_BBOX, HSR_CENTER_LAT, HSR_CENTER_LNG
from .models import Company
from .scoring import score_company
from .sources.csv_seed import fetch_csv_seed
from .sources.google_places import DEFAULT_QUERIES, fetch_google_places
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


def load_queries(args: argparse.Namespace) -> list[str]:
    queries: list[str] = []
    if args.queries_file:
        for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(line)
    queries.extend(args.query or [])
    return queries or list(DEFAULT_QUERIES)


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
    all_companies: list[Company] = []
    source_counts: dict[str, int] = {}

    if "openstreetmap" in requested_sources:
        companies = _safe_fetch(
            "OpenStreetMap/Overpass",
            lambda: fetch_osm(args.bbox, overpass_url=args.overpass_url, timeout=args.timeout),
            strict=args.strict,
        )
        source_counts["openstreetmap"] = len(companies)
        all_companies.extend(companies)

    if "google_places" in requested_sources:
        api_key = args.google_api_key or os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
        if not api_key:
            print(
                "Skipping Google Places: set GOOGLE_PLACES_API_KEY in .env or pass --google-api-key.",
                file=sys.stderr,
            )
            source_counts["google_places"] = 0
        else:
            queries = load_queries(args)
            companies = _safe_fetch(
                "Google Places",
                lambda: fetch_google_places(
                    api_key,
                    queries=queries,
                    lat=args.lat,
                    lng=args.lng,
                    radius_m=args.radius_m,
                    max_pages=args.max_pages,
                    include_details=not args.no_google_details,
                    include_closed=args.include_closed,
                    timeout=args.timeout,
                ),
                strict=args.strict,
            )
            source_counts["google_places"] = len(companies)
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
    filtered.sort(key=lambda company: (-company.tech_score, company.name.lower()))
    if args.limit:
        filtered = filtered[: args.limit]

    out_csv = args.out_csv or os.getenv("OUTPUT_CSV") or "results/hsr_tech_companies.csv"
    out_json = args.out_json or os.getenv("OUTPUT_JSON") or "results/hsr_tech_companies.json"
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
            print(f"  {company.tech_score:>5.1f} {company.confidence:<6} {company.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hsr-tech-finder",
        description="Find tech-company candidates around HSR Layout, Bengaluru.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    find = subparsers.add_parser("find", help="collect and export company candidates")
    find.add_argument("--sources", default="osm,google", help="comma-separated: osm,google,csv")
    find.add_argument("--env", default=".env", help="dotenv file containing GOOGLE_PLACES_API_KEY")
    find.add_argument("--lat", type=float, default=HSR_CENTER_LAT, help="search center latitude")
    find.add_argument("--lng", type=float, default=HSR_CENTER_LNG, help="search center longitude")
    find.add_argument("--radius-m", type=int, default=3000, help="Google Places search radius in metres")
    find.add_argument(
        "--bbox",
        type=parse_bbox,
        default=HSR_BBOX,
        help="OSM bbox as south,west,north,east",
    )
    find.add_argument("--overpass-url", default=OVERPASS_URL, help="Overpass API endpoint")
    find.add_argument("--google-api-key", default=None, help="Google Places API key")
    find.add_argument("--query", action="append", help="extra Google Places query; can be repeated")
    find.add_argument("--queries-file", help="file with one Google Places query per line")
    find.add_argument("--max-pages", type=int, default=3, help="Google pages per query; max useful value is 3")
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
