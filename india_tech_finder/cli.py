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
    SearchCell,
    SearchPoint,
    filter_regions,
    grid_cells_for_region,
    grid_points_for_region,
    load_regions_file,
)
from .scoring import score_company
from .sources.csv_seed import fetch_csv_seed
from .sources.google_places import DEFAULT_QUERY_TEMPLATES, fetch_google_places, render_queries
from .sources.google_places_new import (
    DEFAULT_NEARBY_TYPES,
    DEFAULT_TEXT_QUERIES,
    fetch_google_places_new,
)
from .sources.osm import OVERPASS_URL, fetch_osm
from .sources.web_search import SEARCH_QUERY_TEMPLATES, fetch_web_search
from .job_watcher import (
    DEFAULT_LOCATION_KEYWORDS,
    DEFAULT_TITLE_KEYWORDS,
    load_jobs,
    poll_job_sources,
    write_jobs,
)
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
        "google_new": "google_places_new",
        "places_new": "google_places_new",
        "google_places_new": "google_places_new",
        "osm": "openstreetmap",
        "openstreetmap": "openstreetmap",
        "csv": "seed_csv",
        "seed": "seed_csv",
        "seed_csv": "seed_csv",
        "search": "web_search",
        "web": "web_search",
        "web_search": "web_search",
        "bing": "web_search",
        "serpapi": "web_search",
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


def _load_template_file(path: str | Path) -> list[str]:
    templates: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            templates.append(line)
    return templates


def load_query_templates(args: argparse.Namespace) -> list[str]:
    queries: list[str] = []
    if args.queries_file:
        queries.extend(_load_template_file(args.queries_file))
    queries.extend(args.query or [])
    return queries or list(DEFAULT_QUERY_TEMPLATES)


def load_web_query_templates(args: argparse.Namespace) -> list[str]:
    queries: list[str] = []
    if args.web_queries_file:
        queries.extend(_load_template_file(args.web_queries_file))
    queries.extend(args.web_query or [])
    return queries or list(SEARCH_QUERY_TEMPLATES)


def load_google_new_text_queries(args: argparse.Namespace) -> list[str]:
    queries: list[str] = []
    if args.google_new_text_queries_file:
        queries.extend(_load_template_file(args.google_new_text_queries_file))
    queries.extend(args.google_new_text_query or [])
    return queries or list(DEFAULT_TEXT_QUERIES)


def parse_csv_values(text: str) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


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


def select_google_new_cell_batches(
    regions: list[Region],
    args: argparse.Namespace,
    *,
    batch_index: int,
) -> tuple[dict[str, list[SearchCell]], int, str]:
    all_items: list[tuple[str, SearchCell]] = []
    for region in regions:
        cells = grid_cells_for_region(region, cell_size_m=args.google_new_grid_size_m)
        for cell in cells:
            all_items.append((region.id, cell))

    selected, batch_label = select_batch(
        all_items,
        batch_size=args.google_new_cell_batch_size,
        batch_index=batch_index,
    )
    grouped: dict[str, list[SearchCell]] = defaultdict(list)
    for region_id, cell in selected:
        grouped[region_id].append(cell)
    return dict(grouped), len(all_items), batch_label


def build_web_locations(
    region: Region,
    args: argparse.Namespace,
    grouped_zones: dict[str, list[TechZone]],
) -> list[str]:
    region_locations = [region.query]
    zone_locations = [zone.query for zone in grouped_zones.get(region.id, [])]

    if args.web_search_granularity == "region":
        locations = region_locations
    elif args.web_search_granularity == "zones":
        locations = zone_locations or region_locations
    else:
        locations = region_locations + zone_locations

    seen = set()
    output = []
    for location in locations:
        normalized = " ".join(str(location).split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def select_web_location_batches(
    regions: list[Region],
    args: argparse.Namespace,
    *,
    grouped_zones: dict[str, list[TechZone]],
    batch_index: int,
) -> tuple[dict[str, list[str]], int, str]:
    all_items: list[tuple[str, str]] = []
    for region in regions:
        for location in build_web_locations(region, args, grouped_zones):
            all_items.append((region.id, location))

    selected, batch_label = select_batch(
        all_items,
        batch_size=args.web_location_batch_size,
        batch_index=batch_index,
    )
    grouped: dict[str, list[str]] = defaultdict(list)
    for region_id, location in selected:
        grouped[region_id].append(location)
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
    google_new_text_queries = load_google_new_text_queries(args)
    google_new_nearby_types = parse_csv_values(args.google_new_nearby_types) or list(DEFAULT_NEARBY_TYPES)
    web_query_templates = load_web_query_templates(args)
    tech_zones = load_tech_zones(args.tech_zones_file)
    grouped_zones = zones_by_region(tech_zones)

    google_api_key = None
    google_points_by_region: dict[str, list[SearchPoint]] = {}
    total_google_points = 0
    google_point_batch_label = "all"
    google_new_cells_by_region: dict[str, list[SearchCell]] = {}
    total_google_new_cells = 0
    google_new_cell_batch_label = "all"
    if "google_places" in requested_sources or "google_places_new" in requested_sources:
        google_api_key = args.google_api_key or os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
        if not google_api_key:
            print(
                "Skipping Google Places: set GOOGLE_PLACES_API_KEY in .env or pass --google-api-key.",
                file=sys.stderr,
            )
            if "google_places" in requested_sources:
                source_counts["google_places"] = 0
            if "google_places_new" in requested_sources:
                source_counts["google_places_new"] = 0
        else:
            if args.google_point_batch_index is not None:
                point_batch_index = args.google_point_batch_index
            else:
                # When only a few regions are processed per run, advance the
                # point batch once per full region cycle. This prevents a city
                # from repeatedly getting the same grid slice forever.
                point_batch_index = args.batch_index // region_batch_count
            if "google_places" in requested_sources:
                google_points_by_region, total_google_points, google_point_batch_label = select_google_point_batches(
                    regions,
                    args,
                    grouped_zones=grouped_zones,
                    point_batch_index=point_batch_index,
                )
            if "google_places_new" in requested_sources:
                google_new_batch_index = (
                    args.google_new_cell_batch_index
                    if args.google_new_cell_batch_index is not None
                    else args.batch_index // region_batch_count
                )
                google_new_cells_by_region, total_google_new_cells, google_new_cell_batch_label = select_google_new_cell_batches(
                    regions,
                    args,
                    batch_index=google_new_batch_index,
                )

    web_search_enabled = False
    web_locations_by_region: dict[str, list[str]] = {}
    total_web_locations = 0
    web_location_batch_label = "all"
    bing_search_api_key = args.bing_search_api_key or os.getenv("BING_SEARCH_API_KEY")
    serpapi_api_key = args.serpapi_api_key or os.getenv("SERPAPI_API_KEY")
    if "web_search" in requested_sources:
        if not bing_search_api_key and not serpapi_api_key:
            print(
                "Skipping web search: set BING_SEARCH_API_KEY or SERPAPI_API_KEY, or pass --bing-search-api-key/--serpapi-api-key.",
                file=sys.stderr,
            )
            source_counts["web_search"] = 0
        else:
            web_search_enabled = True
            web_batch_index = args.web_location_batch_index if args.web_location_batch_index is not None else args.batch_index
            web_locations_by_region, total_web_locations, web_location_batch_label = select_web_location_batches(
                regions,
                args,
                grouped_zones=grouped_zones,
                batch_index=web_batch_index,
            )

    print(f"Selected {len(regions)}/{len(all_regions)} region(s), region batch: {region_batch_label}")
    print(f"Regions: {', '.join(region.label for region in regions)}")
    if google_api_key and "google_places" in requested_sources:
        selected_points = sum(len(points) for points in google_points_by_region.values())
        max_text_searches = selected_points * len(query_templates) * max(args.max_pages, 1)
        print(
            "Google classic plan: "
            f"granularity={args.granularity}, points={selected_points}/{total_google_points}, "
            f"point batch={google_point_batch_label}, queries={len(query_templates)}, "
            f"max text-search requests={max_text_searches}"
        )
    if google_api_key and "google_places_new" in requested_sources:
        selected_cells = sum(len(cells) for cells in google_new_cells_by_region.values())
        mode = args.google_new_mode
        per_cell_calls = 0
        if mode in {"text", "both"}:
            per_cell_calls += len(google_new_text_queries) * max(args.google_new_max_pages, 1)
        if mode in {"nearby", "both"}:
            per_cell_calls += len(google_new_nearby_types)
        print(
            "Google Places New plan: "
            f"cells={selected_cells}/{total_google_new_cells}, cell batch={google_new_cell_batch_label}, "
            f"grid={args.google_new_grid_size_m}m, mode={mode}, "
            f"max discovery requests={selected_cells * per_cell_calls} before adaptive splits"
        )
    if web_search_enabled:
        selected_locations = sum(len(locations) for locations in web_locations_by_region.values())
        provider = args.search_provider
        if provider == "auto":
            provider = "bing" if bing_search_api_key else "serpapi"
        print(
            "Web search plan: "
            f"provider={provider}, locations={selected_locations}/{total_web_locations}, "
            f"location batch={web_location_batch_label}, queries={len(web_query_templates)}, "
            f"max results/query={args.web_max_results}"
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

        if "google_places_new" in requested_sources and google_api_key:
            cells = google_new_cells_by_region.get(region.id, [])
            if not cells:
                print(f"Skipping Google Places New ({region.label}): no cells in this batch")
            else:
                companies = _safe_fetch(
                    f"Google Places New ({region.label}, {len(cells)} cell(s), grid={args.google_new_grid_size_m}m)",
                    lambda region=region, cells=cells: fetch_google_places_new(
                        google_api_key,
                        cells=cells,
                        city=region.city,
                        region=region.state,
                        country=region.country,
                        text_queries=google_new_text_queries,
                        nearby_types=google_new_nearby_types,
                        mode=args.google_new_mode,
                        page_size=args.google_new_page_size,
                        max_pages=args.google_new_max_pages,
                        nearby_max_results=args.google_new_nearby_max_results,
                        adaptive=not args.no_google_new_adaptive,
                        adaptive_depth=args.google_new_adaptive_depth,
                        min_cell_size_m=args.google_new_min_cell_size_m,
                        timeout=args.timeout,
                        max_retries=args.google_max_retries,
                        retry_backoff_s=args.retry_backoff_s,
                        request_sleep_s=args.google_new_request_sleep_s,
                    ),
                    strict=args.strict,
                )
                source_counts["google_places_new"] = source_counts.get("google_places_new", 0) + len(companies)
                all_companies.extend(companies)

        if "web_search" in requested_sources and web_search_enabled:
            web_locations = web_locations_by_region.get(region.id, [])
            if not web_locations:
                print(f"Skipping web search ({region.label}): no locations in this batch")
                continue
            companies = _safe_fetch(
                f"Web search ({region.label}, {len(web_locations)} location(s))",
                lambda region=region, web_locations=web_locations: fetch_web_search(
                    provider=args.search_provider,
                    bing_api_key=bing_search_api_key,
                    serpapi_api_key=serpapi_api_key,
                    bing_endpoint=args.bing_search_endpoint,
                    serpapi_endpoint=args.serpapi_endpoint,
                    locations=web_locations,
                    city=region.city,
                    region=region.state,
                    country=region.country,
                    query_templates=web_query_templates,
                    max_results=args.web_max_results,
                    timeout=args.timeout,
                    request_sleep_s=args.web_request_sleep_s,
                ),
                strict=args.strict,
            )
            source_counts["web_search"] = source_counts.get("web_search", 0) + len(companies)
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


def cmd_watch_jobs(args: argparse.Namespace) -> int:
    load_dotenv(args.env)
    companies = read_json(args.companies_json)
    previous_jobs = load_jobs(args.jobs_json)
    location_keywords = args.location_keyword or list(DEFAULT_LOCATION_KEYWORDS)
    title_keywords = args.title_keyword or list(DEFAULT_TITLE_KEYWORDS)

    print(f"Loaded {len(companies)} companies from {args.companies_json}")
    print(f"Loaded {len(previous_jobs)} previous jobs from {args.jobs_json}")
    all_jobs, new_jobs, new_matching, stats = poll_job_sources(
        companies,
        previous_jobs=previous_jobs,
        batch_size=args.batch_size,
        batch_index=args.batch_index,
        timeout=args.timeout,
        max_pages=args.max_pages,
        request_sleep_s=args.request_sleep_s,
        location_keywords=location_keywords,
        title_keywords=title_keywords,
    )

    jobs_path = write_jobs(args.jobs_json, all_jobs)
    new_jobs_path = write_jobs(args.new_jobs_json, new_jobs)
    new_matching_path = write_jobs(args.new_matching_jobs_json, new_matching)

    print("\nJob watcher done")
    print(f"  Career sources: {stats['selected_sources']}/{stats['total_sources']} batch={stats['batch']}")
    print(f"  Successful sources: {stats['successful_sources']}")
    print(f"  Failed sources: {stats['failed_sources']}")
    print(f"  Total jobs: {stats['current_total_jobs']}")
    print(f"  New jobs: {stats['new_jobs']}")
    print(f"  New matching jobs: {stats['new_matching_jobs']}")
    print(f"  Jobs JSON: {jobs_path}")
    print(f"  New jobs JSON: {new_jobs_path}")
    print(f"  New matching jobs JSON: {new_matching_path}")
    if stats.get("failures"):
        print("\nSample failures:")
        for failure in stats["failures"][:5]:
            print(f"  {failure.get('company')} ({failure.get('provider')}): {failure.get('error')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="india-tech-finder",
        description="Find tech-company candidates across Indian tech hubs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    find = subparsers.add_parser("find", help="collect and export company candidates")
    find.add_argument("--sources", default="osm,google_new,search", help="comma-separated: osm,google,google_new,search,csv")
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
    find.add_argument("--google-new-grid-size-m", type=int, default=1000, help="Places API New rectangle grid cell size")
    find.add_argument("--google-new-cell-batch-size", type=int, help="process only N Places API New cells in this run")
    find.add_argument("--google-new-cell-batch-index", type=int, help="override Places API New cell batch index")
    find.add_argument("--google-new-mode", choices=["text", "nearby", "both"], default="both", help="Places API New discovery mode")
    find.add_argument("--google-new-page-size", type=int, default=20, help="Places API New Text Search page size")
    find.add_argument("--google-new-max-pages", type=int, default=1, help="Places API New Text Search pages per query")
    find.add_argument("--google-new-nearby-max-results", type=int, default=20, help="Places API New Nearby max results per type")
    find.add_argument("--google-new-nearby-types", default=",".join(DEFAULT_NEARBY_TYPES), help="comma-separated Nearby Search includedTypes")
    find.add_argument("--google-new-text-query", action="append", help="extra Places API New Text Search query; can be repeated")
    find.add_argument("--google-new-text-queries-file", help="file with one Places API New Text Search query per line")
    find.add_argument("--google-new-request-sleep-s", type=float, default=0.2, help="sleep between Places API New requests")
    find.add_argument("--google-new-adaptive-depth", type=int, default=1, help="split capped cells recursively up to this depth")
    find.add_argument("--google-new-min-cell-size-m", type=int, default=250, help="do not split cells below this size")
    find.add_argument("--no-google-new-adaptive", action="store_true", help="disable adaptive splitting for capped Places API New cells")
    find.add_argument("--search-provider", choices=["auto", "bing", "serpapi"], default="auto", help="official search API provider")
    find.add_argument("--bing-search-api-key", default=None, help="Bing Web Search API key")
    find.add_argument("--bing-search-endpoint", default="https://api.bing.microsoft.com/v7.0/search", help="Bing Web Search endpoint")
    find.add_argument("--serpapi-api-key", default=None, help="SerpAPI key")
    find.add_argument("--serpapi-endpoint", default="https://serpapi.com/search.json", help="SerpAPI endpoint")
    find.add_argument("--web-search-granularity", choices=["region", "zones", "hybrid"], default="hybrid", help="locations used for web search queries")
    find.add_argument("--web-location-batch-size", type=int, help="process only N web-search locations in this run")
    find.add_argument("--web-location-batch-index", type=int, help="override web-search location batch index")
    find.add_argument("--web-max-results", type=int, default=10, help="max web search results per query")
    find.add_argument("--web-request-sleep-s", type=float, default=0.5, help="sleep between web search API requests")
    find.add_argument("--web-query", action="append", help="extra web search query/template; use {location}, {city}, {state}, {country}")
    find.add_argument("--web-queries-file", help="file with one web search query/template per line")
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

    watch = subparsers.add_parser("watch-jobs", help="poll careers/ATS feeds and diff new jobs")
    watch.add_argument("--env", default=".env", help="dotenv file")
    watch.add_argument("--companies-json", default="results/india_tech_companies.json", help="companies JSON input")
    watch.add_argument("--jobs-json", default="results/jobs.json", help="persistent jobs JSON output/input")
    watch.add_argument("--new-jobs-json", default="results/new_jobs.json", help="new jobs from this run")
    watch.add_argument("--new-matching-jobs-json", default="results/new_matching_jobs.json", help="new jobs matching watch filters")
    watch.add_argument("--batch-size", type=int, default=10, help="career sources to poll in this run")
    watch.add_argument("--batch-index", type=int, default=0, help="rotating batch index")
    watch.add_argument("--max-pages", type=int, default=5, help="max pages for paginated ATS APIs")
    watch.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    watch.add_argument("--request-sleep-s", type=float, default=0.5, help="sleep between career source polls")
    watch.add_argument("--location-keyword", action="append", help="location keyword for matching jobs; can be repeated")
    watch.add_argument("--title-keyword", action="append", help="title keyword for matching jobs; can be repeated")
    watch.set_defaults(func=cmd_watch_jobs)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
