from __future__ import annotations

import time
from typing import Dict, Iterable, Optional, Sequence, Tuple

import requests

from ..models import Company


TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
SearchPoint = Tuple[float, float, str]  # lat, lng, label

# Default templates intentionally do not include a city. City-level searches add
# the city automatically, while grid searches rely on lat/lng/radius for local
# coverage and avoid Google returning only the top city-wide results.
DEFAULT_QUERY_TEMPLATES = [
    "software company",
    "IT company",
    "technology company",
    "SaaS company",
    "startup",
    "software development company",
    "AI company",
    "fintech company",
]

# Backwards-compatible name used by older imports.
DEFAULT_QUERIES = DEFAULT_QUERY_TEMPLATES


class GooglePlacesError(RuntimeError):
    pass


def _sleep_for_retry(base_delay_s: float, attempt: int) -> None:
    delay = min(base_delay_s * (2 ** attempt), 60)
    time.sleep(delay)


def _request_json(
    session: requests.Session,
    url: str,
    params: dict,
    timeout: int,
    *,
    max_retries: int = 5,
    retry_backoff_s: float = 2.0,
    request_sleep_s: float = 0.0,
) -> dict:
    for attempt in range(max_retries + 1):
        if request_sleep_s > 0:
            time.sleep(request_sleep_s)

        response = session.get(
            url,
            params=params,
            headers={"User-Agent": "india-tech-company-finder/0.7"},
            timeout=timeout,
        )

        if response.status_code == 429:
            if attempt < max_retries:
                _sleep_for_retry(retry_backoff_s, attempt)
                continue
            raise GooglePlacesError("Google Places HTTP 429 rate/quota limit")

        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")
        if status in {"OK", "ZERO_RESULTS"}:
            return payload
        if status in {"OVER_QUERY_LIMIT", "RESOURCE_EXHAUSTED"} and attempt < max_retries:
            _sleep_for_retry(retry_backoff_s, attempt)
            continue
        raise GooglePlacesError(f"Google Places returned {status}: {payload.get('error_message', '')}")

    return {"status": "ZERO_RESULTS", "results": []}


def _text_search_page(
    session: requests.Session,
    api_key: str,
    *,
    query: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_m: int = 3000,
    page_token: Optional[str] = None,
    timeout: int = 30,
    max_retries: int = 5,
    retry_backoff_s: float = 2.0,
    request_sleep_s: float = 0.0,
) -> dict:
    if page_token:
        params = {"pagetoken": page_token, "key": api_key}
    else:
        params = {
            "query": query,
            "location": f"{lat},{lng}",
            "radius": radius_m,
            "key": api_key,
        }

    # next_page_token can take a short time to become valid.
    for attempt in range(4):
        try:
            return _request_json(
                session,
                TEXT_SEARCH_URL,
                params,
                timeout,
                max_retries=max_retries,
                retry_backoff_s=retry_backoff_s,
                request_sleep_s=request_sleep_s,
            )
        except GooglePlacesError as exc:
            if page_token and "INVALID_REQUEST" in str(exc) and attempt < 3:
                time.sleep(2)
                continue
            raise

    return {"status": "ZERO_RESULTS", "results": []}


def _place_details(
    session: requests.Session,
    api_key: str,
    place_id: str,
    timeout: int = 30,
    *,
    max_retries: int = 5,
    retry_backoff_s: float = 2.0,
    request_sleep_s: float = 0.0,
) -> dict:
    params = {
        "place_id": place_id,
        "fields": "place_id,name,formatted_address,geometry,website,formatted_phone_number,international_phone_number,types,business_status,url",
        "key": api_key,
    }
    payload = _request_json(
        session,
        DETAILS_URL,
        params,
        timeout,
        max_retries=max_retries,
        retry_backoff_s=retry_backoff_s,
        request_sleep_s=request_sleep_s,
    )
    return payload.get("result", {})


def _value(*values):
    for value in values:
        if value:
            return value
    return ""


def _point_query_text(point_label: str) -> str:
    if point_label.startswith("zone:"):
        return point_label[len("zone:"):].strip()
    return ""


def _query_for_point(query: str, point_label: str) -> str:
    point_query = _point_query_text(point_label)
    if point_query and point_query.lower() not in query.lower():
        return f"{query} {point_query}".strip()
    return query


def render_queries(
    templates: Iterable[str],
    *,
    city: str,
    state: str = "",
    country: str = "India",
    include_city: bool = True,
) -> list[str]:
    """Render query templates for a region and remove duplicates."""
    rendered: list[str] = []
    seen = set()
    values = {
        "city": city,
        "city_name": city,
        "state": state,
        "country": country,
    }
    for template in templates:
        query = template.strip()
        if not query:
            continue
        if "{" in query and "}" in query:
            query = query.format(**values)
        elif include_city and city and city.lower() not in query.lower():
            query = f"{query} {city}"
        normalized = " ".join(query.split()).lower()
        if normalized not in seen:
            seen.add(normalized)
            rendered.append(" ".join(query.split()))
    return rendered


def _merge_list(existing: list, incoming: Sequence) -> list:
    seen = set(existing)
    for value in incoming:
        if value and value not in seen:
            existing.append(value)
            seen.add(value)
    return existing


def _company_from_result(
    result: dict,
    query: str,
    details: Optional[dict] = None,
    *,
    city: str = "",
    region: str = "",
    country: str = "India",
    search_point: str = "",
) -> Company:
    details = details or {}
    merged = {**result, **details}
    geometry = merged.get("geometry") or {}
    location = geometry.get("location") or {}
    place_id = merged.get("place_id", "")
    types = list(dict.fromkeys((details.get("types") or []) + (result.get("types") or [])))

    raw = {
        "google_queries": [query],
        "google_types": types,
        "google_url": merged.get("url", ""),
        "business_status": merged.get("business_status", ""),
    }
    if search_point:
        raw["google_search_points"] = [search_point]

    return Company(
        name=_value(merged.get("name"), result.get("name")),
        city=city,
        region=region,
        country=country,
        address=_value(merged.get("formatted_address"), result.get("formatted_address")),
        lat=location.get("lat"),
        lng=location.get("lng"),
        website=_value(merged.get("website")),
        phone=_value(merged.get("formatted_phone_number"), merged.get("international_phone_number")),
        categories=types,
        sources=["google_places"],
        source_ids={"google_places": place_id} if place_id else {},
        raw=raw,
    )


def _merge_google_hit(existing: Company, incoming: Company) -> None:
    if len(incoming.name or "") > len(existing.name or ""):
        existing.name = incoming.name
    if not existing.address or len(incoming.address or "") > len(existing.address or ""):
        existing.address = incoming.address
    if existing.lat is None and incoming.lat is not None:
        existing.lat = incoming.lat
    if existing.lng is None and incoming.lng is not None:
        existing.lng = incoming.lng
    existing.categories = _merge_list(existing.categories, incoming.categories)
    existing.raw["google_queries"] = _merge_list(
        existing.raw.setdefault("google_queries", []), incoming.raw.get("google_queries", [])
    )
    existing.raw["google_types"] = _merge_list(
        existing.raw.setdefault("google_types", []), incoming.raw.get("google_types", [])
    )
    existing.raw["google_search_points"] = _merge_list(
        existing.raw.setdefault("google_search_points", []), incoming.raw.get("google_search_points", [])
    )


def _apply_details(company: Company, details: dict) -> None:
    if not details:
        return
    if details.get("name") and len(details["name"]) > len(company.name or ""):
        company.name = details["name"]
    if details.get("formatted_address") and len(details["formatted_address"]) > len(company.address or ""):
        company.address = details["formatted_address"]
    geometry = details.get("geometry") or {}
    location = geometry.get("location") or {}
    if location.get("lat") is not None:
        company.lat = location.get("lat")
    if location.get("lng") is not None:
        company.lng = location.get("lng")
    if details.get("website"):
        company.website = details["website"]
    if details.get("formatted_phone_number") or details.get("international_phone_number"):
        company.phone = details.get("formatted_phone_number") or details.get("international_phone_number")
    company.categories = _merge_list(company.categories, details.get("types") or [])
    company.raw["google_types"] = _merge_list(company.raw.setdefault("google_types", []), details.get("types") or [])
    if details.get("url"):
        company.raw["google_url"] = details["url"]
    if details.get("business_status"):
        company.raw["business_status"] = details["business_status"]


def fetch_google_places(
    api_key: str,
    *,
    queries: Iterable[str] = DEFAULT_QUERIES,
    lat: float,
    lng: float,
    city: str = "",
    region: str = "",
    country: str = "India",
    radius_m: int = 3000,
    max_pages: int = 3,
    include_details: bool = True,
    include_closed: bool = False,
    timeout: int = 30,
    search_points: Optional[Iterable[SearchPoint]] = None,
    max_retries: int = 5,
    retry_backoff_s: float = 2.0,
    request_sleep_s: float = 0.0,
) -> list[Company]:
    """Fetch candidates through Google Places Text Search.

    Requires a Google API key with Places API enabled. The code uses official
    API endpoints rather than scraping Google Maps.
    """
    session = requests.Session()
    by_place_id: Dict[str, Company] = {}
    anonymous: list[Company] = []
    details_cache: Dict[str, dict] = {}
    points = list(search_points or [(lat, lng, "center")])

    for point_lat, point_lng, point_label in points:
        for query in queries:
            page_token = None
            for page_number in range(max_pages):
                if page_token:
                    time.sleep(2)
                effective_query = _query_for_point(query, point_label)
                payload = _text_search_page(
                    session,
                    api_key,
                    query=effective_query,
                    lat=point_lat,
                    lng=point_lng,
                    radius_m=radius_m,
                    page_token=page_token,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff_s=retry_backoff_s,
                    request_sleep_s=request_sleep_s,
                )
                for result in payload.get("results", []):
                    if not include_closed and result.get("business_status") == "CLOSED_PERMANENTLY":
                        continue
                    place_id = result.get("place_id", "")
                    company = _company_from_result(
                        result,
                        effective_query,
                        None,
                        city=city,
                        region=region,
                        country=country,
                        search_point=point_label,
                    )
                    if place_id:
                        if place_id in by_place_id:
                            _merge_google_hit(by_place_id[place_id], company)
                        else:
                            by_place_id[place_id] = company
                    else:
                        anonymous.append(company)

                page_token = payload.get("next_page_token")
                if not page_token:
                    break

    if include_details:
        closed_place_ids = set()
        for place_id, company in list(by_place_id.items()):
            if place_id not in details_cache:
                details_cache[place_id] = _place_details(
                    session,
                    api_key,
                    place_id,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff_s=retry_backoff_s,
                    request_sleep_s=request_sleep_s,
                )
            details = details_cache[place_id]
            if not include_closed and details.get("business_status") == "CLOSED_PERMANENTLY":
                closed_place_ids.add(place_id)
                continue
            _apply_details(company, details)
        for place_id in closed_place_ids:
            by_place_id.pop(place_id, None)

    return list(by_place_id.values()) + anonymous
