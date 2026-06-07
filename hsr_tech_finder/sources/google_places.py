from __future__ import annotations

import time
from typing import Dict, Iterable, Optional

import requests

from ..models import Company


TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

DEFAULT_QUERY_TEMPLATES = [
    "software company {city}",
    "IT company {city}",
    "technology company {city}",
    "SaaS company {city}",
    "startup {city}",
    "software development company {city}",
    "AI company {city}",
    "fintech company {city}",
]

# Backwards-compatible name used by older imports.
DEFAULT_QUERIES = DEFAULT_QUERY_TEMPLATES


class GooglePlacesError(RuntimeError):
    pass


def _request_json(session: requests.Session, url: str, params: dict, timeout: int) -> dict:
    response = session.get(
        url,
        params=params,
        headers={"User-Agent": "india-tech-company-finder/0.2"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    status = payload.get("status")
    if status in {"OK", "ZERO_RESULTS"}:
        return payload
    raise GooglePlacesError(f"Google Places returned {status}: {payload.get('error_message', '')}")


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
            return _request_json(session, TEXT_SEARCH_URL, params, timeout)
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
) -> dict:
    params = {
        "place_id": place_id,
        "fields": "place_id,name,formatted_address,geometry,website,formatted_phone_number,international_phone_number,types,business_status,url",
        "key": api_key,
    }
    payload = _request_json(session, DETAILS_URL, params, timeout)
    return payload.get("result", {})


def _value(*values):
    for value in values:
        if value:
            return value
    return ""


def render_queries(templates: Iterable[str], *, city: str, state: str = "", country: str = "India") -> list[str]:
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
        elif city and city.lower() not in query.lower():
            query = f"{query} {city}"
        normalized = " ".join(query.split()).lower()
        if normalized not in seen:
            seen.add(normalized)
            rendered.append(" ".join(query.split()))
    return rendered


def _company_from_result(
    result: dict,
    query: str,
    details: Optional[dict] = None,
    *,
    city: str = "",
    region: str = "",
    country: str = "India",
) -> Company:
    details = details or {}
    merged = {**result, **details}
    geometry = merged.get("geometry") or {}
    location = geometry.get("location") or {}
    place_id = merged.get("place_id", "")
    types = list(dict.fromkeys((details.get("types") or []) + (result.get("types") or [])))

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
        raw={
            "google_queries": [query],
            "google_types": types,
            "google_url": merged.get("url", ""),
            "business_status": merged.get("business_status", ""),
        },
    )


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
) -> list[Company]:
    """Fetch candidates through Google Places Text Search.

    Requires a Google API key with Places API enabled. The code uses official
    API endpoints rather than scraping Google Maps.
    """
    session = requests.Session()
    by_place_id: Dict[str, Company] = {}
    anonymous: list[Company] = []
    details_cache: Dict[str, dict] = {}

    for query in queries:
        page_token = None
        for page_number in range(max_pages):
            if page_token:
                time.sleep(2)
            payload = _text_search_page(
                session,
                api_key,
                query=query,
                lat=lat,
                lng=lng,
                radius_m=radius_m,
                page_token=page_token,
                timeout=timeout,
            )
            for result in payload.get("results", []):
                if not include_closed and result.get("business_status") == "CLOSED_PERMANENTLY":
                    continue
                place_id = result.get("place_id", "")
                details = {}
                if include_details and place_id:
                    if place_id not in details_cache:
                        details_cache[place_id] = _place_details(session, api_key, place_id, timeout=timeout)
                    details = details_cache[place_id]
                    if not include_closed and details.get("business_status") == "CLOSED_PERMANENTLY":
                        continue

                company = _company_from_result(
                    result,
                    query,
                    details,
                    city=city,
                    region=region,
                    country=country,
                )
                if place_id:
                    if place_id in by_place_id:
                        existing_queries = by_place_id[place_id].raw.setdefault("google_queries", [])
                        if query not in existing_queries:
                            existing_queries.append(query)
                    else:
                        by_place_id[place_id] = company
                else:
                    anonymous.append(company)

            page_token = payload.get("next_page_token")
            if not page_token:
                break

    return list(by_place_id.values()) + anonymous
