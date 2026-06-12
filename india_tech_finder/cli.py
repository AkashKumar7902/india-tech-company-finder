from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable, Optional, TypeVar

from .careers import enrich_companies_with_careers
from .dedupe import dedupe_companies
from .export import read_json, write_csv, write_json
from .models import Company
from .regions import (
    PRESETS,
    Region,
    SearchPoint,
    filter_regions,
    grid_points_for_region,
    load_regions_file,
)
from .scoring import score_company
from .sources.csv_seed import fetch_csv_seed
from .sources.google_places import DEFAULT_QUERY_TEMPLATES, fetch_google_places, render_queries
from .sources.osm import OVERPASS_URL, fetch_osm
from .zones import TechZone, load_tech_zones, tech_zone_points_for_region, zones_by_region

T = TypeVar("T")


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


def get_batch_count(item_count: int, batch_size: Optional[int]) -> int:
    if not batch_size or batch_size <= 0 or batch_size >= item_count:
        return 1
    return math.ceil(item_count / batch_size)


def select_batch(items: list[T], *, batch_size: Optional[int], batch_index: int) -> tuple[list[T], str]:
    if not batch_size or batch_size <= 0 or batch_size >= len(items):
        return items, "all"
    batch_count = get_batch_count(len(items), batch_size)
    normalized_index = batch_index % batch_count
    start = normalized_index * batch_size
    end = start + batch_size
    return items[start:end], f"{normalized_index + 1}/{batch_count}"


def _dedupe_points(points: Iterable[SearchPoint]) -> list[SearchPoint]:
    output: list[SearchPoint] = []
    seen = set()
    for lat, lng, label in points:
        key = (round(lat, 5), round(lng, 5), label)
        if key not in seen:
            seen.add(key)
            output.append((lat, lng, label))
    return output


def build_search_points(
    region: Region,
    args: argparse.Namespace,
    grouped_zones: dict[str, list[TechZone]],
) -> tuple[list[SearchPoint], int]:
    if args.granularity == "city":
        return [(region.lat, region.lng, "center")], region.radius_m

    zone_points = tech_zone_points_for_region(region, grouped_zones)
    grid_points = grid_points_for_region(region, spacing_m=args.grid_size_m)

    if args.granularity == "zones":
        # Fallback to the city center if a custom region has no curated zones.
        return (zone_points or [(region.lat, region.lng, "center")]), args.zone_radius_m

    grid_radius_m = args.grid_radius_m or args.grid_size_m
    if args.granularity == "hybrid":
        # Priority order matters for rotating batches: curated zones are scanned
        # before generic grid points, but the grid still covers non-famous areas.
        return _dedupe_points(zone_points + grid_points), max(args.zone_radius_m, grid_radius_m)

    return grid_points, grid_radius_m


def select_google_point_batches(
    regions: list[Region],
    args: argparse.Namespace,
    *,
    grouped_zones: dict[str, list[TechZone]],
    point_batch_index: int,
) -> tuple[dict[str, list[SearchPoint]], int, str]:
    all_items: list[tuple[str, SearchPoint]] = []
    for region in regions:
        points, _ = build_search_points(region, args, grouped_zones)
        for point in points:
            all_items.append((region.id, point))

    selected, batch_label = select_batch(
        all_items,
        batch_size=args.google_point_batch_size,
        batch_index=point_batch_index,
    )
    grouped: dict[str, list[SearchPoint]] = defaultdict(list)
    for region_id, point in selected:
        grouped[region_id].append(point)
    return dict(grouped), len(all_items), batch_label


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
    all_regions = resolve_regions(args)
    region_batch_count = get_batch_count(len(all_regions), args.region_batch_size)
    region_batch_index = args.region_batch_index if args.region_batch_index is not None else args.batch_index
    regions, region_batch_label = select_batch(
        all_regions,
        batch_size=args.region_batch_size,
        batch_index=region_batch_index,
    )

    out_csv = args.out_csv or os.getenv("OUTPUT_CSV") or "results/india_tech_companies.csv"
    out_json = args.out_json or os.getenv("OUTPUT_JSON") or "results/india_tech_companies.json"

    all_companies: list[Company] = []
    source_counts: dict[str, int] = {source: 0 for source in requested_sources}

    if args.merge_existing:
        existing = read_json(out_json)
        if existing:
            print(f"Loaded {len(existing)} existing result(s) from {out_json}")
            all_companies.extend(existing)
            source_counts["existing_results"] = len(existing)

    query_templates = load_query_templates(args)
    tech_zones = load_tech_zones(args.tech_zones_file)
    grouped_zones = zones_by_region(tech_zones)

    google_api_key = None
    google_points_by_region: dict[str, list[SearchPoint]] = {}
    total_google_points = 0
    google_point_batch_label = "all"
    if "google_places" in requested_sources:
        google_api_key = args.google_api_key or os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
        if not google_api_key:
            print(
                "Skipping Google Places: set GOOGLE_PLACES_API_KEY in .env or pass --google-api-key.",
                file=sys.stderr,
            )
            source_counts["google_places"] = 0
        else:
            if args.google_point_batch_index is not None:
                point_batch_index = args.google_point_batch_index
            else:
                # When only a few regions are processed per run, advance the
                # point batch once per full region cycle. This prevents a city
                # from repeatedly getting the same grid slice forever.
                point_batch_index = args.batch_index // region_batch_count
            google_points_by_region, total_google_points, google_point_batch_label = select_google_point_batches(
                regions,
                args,
                grouped_zones=grouped_zones,
                point_batch_index=point_batch_index,
            )

    print(f"Selected {len(regions)}/{len(all_regions)} region(s), region batch: {region_batch_label}")
    print(f"Regions: {', '.join(region.label for region in regions)}")
    if google_api_key:
        selected_points = sum(len(points) for points in google_points_by_region.values())
        max_text_searches = selected_points * len(query_templates) * max(args.max_pages, 1)
        print(
            "Google plan: "
            f"granularity={args.granularity}, points={selected_points}/{total_google_points}, "
            f"point batch={google_point_batch_label}, queries={len(query_templates)}, "
            f"max text-search requests={max_text_searches}"
        )

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
                    max_retries=args.osm_max_retries,
                    retry_backoff_s=args.retry_backoff_s,
                ),
                strict=args.strict,
            )
            source_counts["openstreetmap"] = source_counts.get("openstreetmap", 0) + len(companies)
            all_companies.extend(companies)

        if "google_places" in requested_sources and google_api_key:
            search_points = google_points_by_region.get(region.id, [])
            if not search_points:
                print(f"Skipping Google Places ({region.label}): no points in this batch")
                continue
            _, radius_m = build_search_points(region, args, grouped_zones)
            queries = render_queries(
                query_templates,
                city=region.query,
                state=region.state,
                country=region.country,
                include_city=args.granularity == "city",
            )
            companies = _safe_fetch(
                f"Google Places ({region.label}, {len(search_points)} point(s), radius={radius_m}m)",
                lambda region=region, queries=queries, search_points=search_points, radius_m=radius_m: fetch_google_places(
                    google_api_key,
                    queries=queries,
                    lat=region.lat,
                    lng=region.lng,
                    city=region.city,
                    region=region.state,
                    country=region.country,
                    radius_m=radius_m,
                    max_pages=args.max_pages,
                    include_details=not args.no_google_details,
                    include_closed=args.include_closed,
                    timeout=args.timeout,
                    search_points=search_points,
                    max_retries=args.google_max_retries,
                    retry_backoff_s=args.retry_backoff_s,
                    request_sleep_s=args.google_request_sleep_s,
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

    if args.enrich_careers:
        careers_candidates = [
            company
            for company in filtered
            if company.website and (args.refresh_careers or not company.careers_last_checked)
        ]
        careers_batch_index = args.careers_batch_index if args.careers_batch_index is not None else args.batch_index
        careers_batch, careers_batch_label = select_batch(
            careers_candidates,
            batch_size=args.careers_batch_size,
            batch_index=careers_batch_index,
        )
        print(
            f"\nCareers enrichment: {len(careers_batch)}/{len(careers_candidates)} "
            f"company website(s), batch={careers_batch_label}"
        )
        enriched_count = enrich_companies_with_careers(
            careers_batch,
            timeout=args.careers_timeout,
            max_pages=args.careers_max_pages,
            request_sleep_s=args.careers_request_sleep_s,
        )
        print(f"  Enriched careers metadata for {enriched_count} company website(s)")

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
    find.add_argument("--radius-m", type=int, help="override city-mode Google radius in metres")
    find.add_argument("--bbox", type=parse_bbox, help="custom OSM bbox as south,west,north,east")
    find.add_argument("--granularity", choices=["city", "zones", "grid", "hybrid"], default="hybrid", help="Google search mode")
    find.add_argument("--tech-zones-file", help="custom curated tech zones JSON file")
    find.add_argument("--zone-radius-m", type=int, default=4000, help="Google radius for curated tech-zone points")
    find.add_argument("--grid-size-m", type=int, default=5000, help="grid spacing for granular Google search")
    find.add_argument("--grid-radius-m", type=int, help="Google radius per grid point; default equals --grid-size-m")
    find.add_argument("--batch-index", type=int, default=0, help="rotating batch index; any integer is accepted")
    find.add_argument("--region-batch-size", type=int, help="process only N regions in this run")
    find.add_argument("--region-batch-index", type=int, help="override region batch index")
    find.add_argument("--google-point-batch-size", type=int, help="process only N Google grid/center points in this run")
    find.add_argument("--google-point-batch-index", type=int, help="override Google point batch index")
    find.add_argument("--merge-existing", action="store_true", help="merge with existing JSON output before exporting")
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
    find.add_argument("--google-request-sleep-s", type=float, default=0.0, help="sleep between Google API requests")
    find.add_argument("--google-max-retries", type=int, default=5, help="Google 429/quota retry attempts")
    find.add_argument("--enrich-careers", action="store_true", help="discover careers page/provider/API for companies with websites")
    find.add_argument("--refresh-careers", action="store_true", help="re-check careers metadata even if already checked")
    find.add_argument("--careers-batch-size", type=int, help="process only N company websites for careers enrichment")
    find.add_argument("--careers-batch-index", type=int, help="override careers enrichment batch index")
    find.add_argument("--careers-timeout", type=int, default=10, help="careers enrichment HTTP timeout seconds")
    find.add_argument("--careers-max-pages", type=int, default=5, help="max homepage/careers candidate pages to inspect per company")
    find.add_argument("--careers-request-sleep-s", type=float, default=0.5, help="sleep between company career-site checks")
    find.add_argument("--osm-max-retries", type=int, default=3, help="Overpass HTTP 429 retry attempts")
    find.add_argument("--retry-backoff-s", type=float, default=2.0, help="base exponential retry backoff seconds")
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
