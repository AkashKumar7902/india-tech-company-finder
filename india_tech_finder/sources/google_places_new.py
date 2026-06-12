from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import requests

from ..models import Company
from ..regions import SearchCell


TEXT_SEARCH_NEW_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_SEARCH_NEW_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Low-cost discovery fields only. Do not include websiteUri, phone numbers,
# rating, reviews, opening hours, etc. in this first pass.
PLACE_DISCOVERY_FIELDS = [
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.primaryType",
    "places.types",
    "places.googleMapsUri",
]
TEXT_DISCOVERY_FIELD_MASK = ",".join(PLACE_DISCOVERY_FIELDS + ["nextPageToken"])
NEARBY_DISCOVERY_FIELD_MASK = ",".join(PLACE_DISCOVERY_FIELDS)

DEFAULT_TEXT_QUERIES = [
    "software company",
    "IT company",
    "technology company",
    "software development company",
    "SaaS company",
    "startup",
    "AI company",
    "fintech company",
]

DEFAULT_NEARBY_TYPES = ["corporate_office", "business_center", "coworking_space"]


class GooglePlacesNewError(RuntimeError):
    pass


@dataclass(frozen=True)
class CellResultStats:
    calls: int = 0
    hits_cap: bool = False


def _sleep_for_retry(base_delay_s: float, attempt: int) -> None:
    time.sleep(min(base_delay_s * (2 ** attempt), 60))


def _request_json(
    session: requests.Session,
    url: str,
    api_key: str,
    payload: dict,
    *,
    field_mask: str = TEXT_DISCOVERY_FIELD_MASK,
    timeout: int = 30,
    max_retries: int = 5,
    retry_backoff_s: float = 2.0,
    request_sleep_s: float = 0.0,
) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
        "User-Agent": "india-tech-company-finder/0.7",
    }
    for attempt in range(max_retries + 1):
        if request_sleep_s > 0:
            time.sleep(request_sleep_s)
        response = session.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code in {429, 500, 502, 503, 504}:
            if attempt < max_retries:
                _sleep_for_retry(retry_backoff_s, attempt)
                continue
            raise GooglePlacesNewError(f"Google Places New HTTP {response.status_code}: {response.text[:300]}")
        response.raise_for_status()
        return response.json()
    return {"places": []}


def _cell_center_radius(cell: SearchCell) -> tuple[float, float, float]:
    south, west, north, east, _ = cell
    lat = (south + north) / 2
    lng = (west + east) / 2
    lat_m = abs(north - south) * 111_000
    lng_m = abs(east - west) * 111_000 * max(math.cos(math.radians(lat)), 0.1)
    radius = max(50.0, math.sqrt(lat_m**2 + lng_m**2) / 2)
    return lat, lng, radius


def _split_cell(cell: SearchCell) -> list[SearchCell]:
    south, west, north, east, label = cell
    mid_lat = (south + north) / 2
    mid_lng = (west + east) / 2
    return [
        (south, west, mid_lat, mid_lng, f"{label}-a"),
        (south, mid_lng, mid_lat, east, f"{label}-b"),
        (mid_lat, west, north, mid_lng, f"{label}-c"),
        (mid_lat, mid_lng, north, east, f"{label}-d"),
    ]


def _cell_size_m(cell: SearchCell) -> float:
    south, west, north, east, _ = cell
    lat = (south + north) / 2
    lat_m = abs(north - south) * 111_000
    lng_m = abs(east - west) * 111_000 * max(math.cos(math.radians(lat)), 0.1)
    return max(lat_m, lng_m)


def _place_value(place: dict, key: str, default=""):
    value = place.get(key, default)
    return default if value is None else value


def _place_to_company(
    place: dict,
    *,
    city: str,
    region: str,
    country: str,
    query: str,
    cell_label: str,
    mode: str,
) -> Optional[Company]:
    display_name = place.get("displayName") or {}
    name = display_name.get("text") or place.get("name") or ""
    if not name:
        return None
    location = place.get("location") or {}
    types = list(dict.fromkeys(([place.get("primaryType")] if place.get("primaryType") else []) + (place.get("types") or [])))
    place_id = place.get("id") or ""
    return Company(
        name=name,
        city=city,
        region=region,
        country=country,
        address=_place_value(place, "formattedAddress", ""),
        lat=location.get("latitude"),
        lng=location.get("longitude"),
        categories=types,
        sources=["google_places_new"],
        source_ids={"google_places_new": place_id} if place_id else {},
        raw={
            "google_places_new_queries": [query] if query else [],
            "google_places_new_cell": cell_label,
            "google_places_new_mode": mode,
            "google_places_new_types": types,
            "google_maps_uri": place.get("googleMapsUri", ""),
        },
    )


def _merge_company_hit(existing: Company, incoming: Company) -> None:
    if len(incoming.name or "") > len(existing.name or ""):
        existing.name = incoming.name
    if not existing.address or len(incoming.address or "") > len(existing.address or ""):
        existing.address = incoming.address
    if existing.lat is None and incoming.lat is not None:
        existing.lat = incoming.lat
    if existing.lng is None and incoming.lng is not None:
        existing.lng = incoming.lng
    seen = set(existing.categories)
    for value in incoming.categories:
        if value and value not in seen:
            existing.categories.append(value)
            seen.add(value)
    existing.raw.setdefault("google_places_new_queries", [])
    for query in incoming.raw.get("google_places_new_queries", []):
        if query and query not in existing.raw["google_places_new_queries"]:
            existing.raw["google_places_new_queries"].append(query)
    cells = existing.raw.setdefault("google_places_new_cells", [])
    cell = incoming.raw.get("google_places_new_cell")
    if cell and cell not in cells:
        cells.append(cell)


def _text_search_cell_query(
    session: requests.Session,
    api_key: str,
    cell: SearchCell,
    query: str,
    *,
    page_size: int,
    max_pages: int,
    timeout: int,
    max_retries: int,
    retry_backoff_s: float,
    request_sleep_s: float,
) -> tuple[list[dict], CellResultStats]:
    south, west, north, east, _ = cell
    places: list[dict] = []
    page_token = ""
    calls = 0
    for _page in range(max_pages):
        payload = {
            "textQuery": query,
            "pageSize": min(max(page_size, 1), 20),
            "rankPreference": "DISTANCE",
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": south, "longitude": west},
                    "high": {"latitude": north, "longitude": east},
                }
            },
        }
        if page_token:
            payload["pageToken"] = page_token
            time.sleep(2)
        data = _request_json(
            session,
            TEXT_SEARCH_NEW_URL,
            api_key,
            payload,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff_s=retry_backoff_s,
            request_sleep_s=request_sleep_s,
        )
        calls += 1
        places.extend(data.get("places", []) or [])
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    hits_cap = bool(page_token) or len(places) >= min(page_size, 20) * max(max_pages, 1)
    return places, CellResultStats(calls=calls, hits_cap=hits_cap)


def _nearby_search_cell_type(
    session: requests.Session,
    api_key: str,
    cell: SearchCell,
    place_type: str,
    *,
    max_results: int,
    timeout: int,
    max_retries: int,
    retry_backoff_s: float,
    request_sleep_s: float,
) -> tuple[list[dict], CellResultStats]:
    lat, lng, radius = _cell_center_radius(cell)
    payload = {
        "includedTypes": [place_type],
        "maxResultCount": min(max(max_results, 1), 20),
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius,
            }
        },
    }
    data = _request_json(
        session,
        NEARBY_SEARCH_NEW_URL,
        api_key,
        payload,
        field_mask=NEARBY_DISCOVERY_FIELD_MASK,
        timeout=timeout,
        max_retries=max_retries,
        retry_backoff_s=retry_backoff_s,
        request_sleep_s=request_sleep_s,
    )
    places = data.get("places", []) or []
    hits_cap = len(places) >= min(max_results, 20)
    return places, CellResultStats(calls=1, hits_cap=hits_cap)


def _fetch_cell_recursive(
    session: requests.Session,
    api_key: str,
    cell: SearchCell,
    *,
    city: str,
    region: str,
    country: str,
    text_queries: Sequence[str],
    nearby_types: Sequence[str],
    mode: str,
    page_size: int,
    max_pages: int,
    nearby_max_results: int,
    adaptive: bool,
    adaptive_depth: int,
    min_cell_size_m: int,
    timeout: int,
    max_retries: int,
    retry_backoff_s: float,
    request_sleep_s: float,
) -> list[Company]:
    by_id: dict[str, Company] = {}
    hit_cap = False

    if mode in {"text", "both"}:
        for query in text_queries:
            places, stats = _text_search_cell_query(
                session,
                api_key,
                cell,
                query,
                page_size=page_size,
                max_pages=max_pages,
                timeout=timeout,
                max_retries=max_retries,
                retry_backoff_s=retry_backoff_s,
                request_sleep_s=request_sleep_s,
            )
            hit_cap = hit_cap or stats.hits_cap
            for place in places:
                company = _place_to_company(
                    place,
                    city=city,
                    region=region,
                    country=country,
                    query=query,
                    cell_label=cell[4],
                    mode="text",
                )
                if not company:
                    continue
                place_id = company.source_ids.get("google_places_new") or f"anon:{company.name}:{company.lat}:{company.lng}"
                if place_id in by_id:
                    _merge_company_hit(by_id[place_id], company)
                else:
                    by_id[place_id] = company

    if mode in {"nearby", "both"}:
        for place_type in nearby_types:
            places, stats = _nearby_search_cell_type(
                session,
                api_key,
                cell,
                place_type,
                max_results=nearby_max_results,
                timeout=timeout,
                max_retries=max_retries,
                retry_backoff_s=retry_backoff_s,
                request_sleep_s=request_sleep_s,
            )
            hit_cap = hit_cap or stats.hits_cap
            for place in places:
                company = _place_to_company(
                    place,
                    city=city,
                    region=region,
                    country=country,
                    query=f"nearby:{place_type}",
                    cell_label=cell[4],
                    mode="nearby",
                )
                if not company:
                    continue
                place_id = company.source_ids.get("google_places_new") or f"anon:{company.name}:{company.lat}:{company.lng}"
                if place_id in by_id:
                    _merge_company_hit(by_id[place_id], company)
                else:
                    by_id[place_id] = company

    if adaptive and hit_cap and adaptive_depth > 0 and _cell_size_m(cell) > min_cell_size_m:
        for subcell in _split_cell(cell):
            for company in _fetch_cell_recursive(
                session,
                api_key,
                subcell,
                city=city,
                region=region,
                country=country,
                text_queries=text_queries,
                nearby_types=nearby_types,
                mode=mode,
                page_size=page_size,
                max_pages=max_pages,
                nearby_max_results=nearby_max_results,
                adaptive=adaptive,
                adaptive_depth=adaptive_depth - 1,
                min_cell_size_m=min_cell_size_m,
                timeout=timeout,
                max_retries=max_retries,
                retry_backoff_s=retry_backoff_s,
                request_sleep_s=request_sleep_s,
            ):
                place_id = company.source_ids.get("google_places_new") or f"anon:{company.name}:{company.lat}:{company.lng}"
                if place_id in by_id:
                    _merge_company_hit(by_id[place_id], company)
                else:
                    by_id[place_id] = company

    return list(by_id.values())


def fetch_google_places_new(
    api_key: str,
    *,
    cells: Iterable[SearchCell],
    city: str,
    region: str,
    country: str = "India",
    text_queries: Iterable[str] = DEFAULT_TEXT_QUERIES,
    nearby_types: Iterable[str] = DEFAULT_NEARBY_TYPES,
    mode: str = "both",
    page_size: int = 20,
    max_pages: int = 1,
    nearby_max_results: int = 20,
    adaptive: bool = True,
    adaptive_depth: int = 1,
    min_cell_size_m: int = 250,
    timeout: int = 30,
    max_retries: int = 5,
    retry_backoff_s: float = 2.0,
    request_sleep_s: float = 0.2,
) -> list[Company]:
    """Fetch Google Places API (New) candidates with restricted grid cells.

    Uses ``places:searchText`` with rectangle ``locationRestriction`` and
    ``places:searchNearby`` with ``rankPreference=DISTANCE``. The field mask is
    deliberately discovery-only to avoid costly enrichment fields.
    """
    mode = mode.lower()
    if mode not in {"text", "nearby", "both"}:
        raise ValueError("google places new mode must be text, nearby, or both")

    session = requests.Session()
    by_id: dict[str, Company] = {}
    for cell in cells:
        for company in _fetch_cell_recursive(
            session,
            api_key,
            cell,
            city=city,
            region=region,
            country=country,
            text_queries=list(text_queries),
            nearby_types=list(nearby_types),
            mode=mode,
            page_size=page_size,
            max_pages=max_pages,
            nearby_max_results=nearby_max_results,
            adaptive=adaptive,
            adaptive_depth=adaptive_depth,
            min_cell_size_m=min_cell_size_m,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff_s=retry_backoff_s,
            request_sleep_s=request_sleep_s,
        ):
            place_id = company.source_ids.get("google_places_new") or f"anon:{company.name}:{company.lat}:{company.lng}"
            if place_id in by_id:
                _merge_company_hit(by_id[place_id], company)
            else:
                by_id[place_id] = company
    return list(by_id.values())
