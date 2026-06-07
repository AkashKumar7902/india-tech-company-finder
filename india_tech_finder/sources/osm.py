from __future__ import annotations

import time
from typing import Iterable, Tuple

import requests

from ..models import Company


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TECH_OSM_REGEX = (
    "software|technolog|technology|technologies|infotech|information technology|"
    "it services|digital|systems|solutions|labs|cloud|data analytics|cyber|"
    "cybersecurity|saas|fintech|startup|app development|web development|"
    "artificial intelligence|machine learning"
)


def build_overpass_query(bbox: Tuple[float, float, float, float], timeout: int = 60) -> str:
    south, west, north, east = bbox
    bbox_text = f"{south},{west},{north},{east}"
    return f"""
[out:json][timeout:{timeout}];
(
  nwr["office"="it"]({bbox_text});
  nwr["name"~"{TECH_OSM_REGEX}",i]({bbox_text});
  nwr["brand"~"{TECH_OSM_REGEX}",i]({bbox_text});
  nwr["operator"~"{TECH_OSM_REGEX}",i]({bbox_text});
  nwr["description"~"{TECH_OSM_REGEX}",i]({bbox_text});
);
out center tags;
""".strip()


def _tag(tags: dict, *names: str) -> str:
    for name in names:
        value = tags.get(name)
        if value:
            return str(value)
    return ""


def _address(tags: dict) -> str:
    explicit = _tag(tags, "addr:full", "address", "contact:address")
    if explicit:
        return explicit
    parts = [
        _tag(tags, "addr:housenumber"),
        _tag(tags, "addr:street"),
        _tag(tags, "addr:suburb", "addr:neighbourhood"),
        _tag(tags, "addr:city"),
        _tag(tags, "addr:postcode"),
    ]
    return ", ".join(part for part in parts if part)


def _categories(tags: dict) -> list[str]:
    interesting = [
        "office",
        "amenity",
        "shop",
        "craft",
        "building",
        "industrial",
        "description",
    ]
    categories = []
    for key in interesting:
        if key in tags:
            categories.append(f"{key}={tags[key]}")
    return categories


def _iter_companies(
    elements: Iterable[dict],
    *,
    city: str = "",
    region: str = "",
    country: str = "India",
) -> Iterable[Company]:
    for element in elements:
        tags = element.get("tags") or {}
        name = _tag(tags, "name", "brand", "operator")
        if not name:
            continue

        center = element.get("center") or {}
        lat = element.get("lat", center.get("lat"))
        lng = element.get("lon", center.get("lon"))
        try:
            lat = float(lat) if lat is not None else None
            lng = float(lng) if lng is not None else None
        except (TypeError, ValueError):
            lat = None
            lng = None

        osm_id = f"{element.get('type')}:{element.get('id')}"
        yield Company(
            name=name,
            city=city,
            region=region,
            country=country,
            address=_address(tags),
            lat=lat,
            lng=lng,
            website=_tag(tags, "contact:website", "website", "url"),
            phone=_tag(tags, "contact:phone", "phone"),
            categories=_categories(tags),
            sources=["openstreetmap"],
            source_ids={"openstreetmap": osm_id},
            raw={"osm_tags": tags},
        )


def fetch_osm(
    bbox: Tuple[float, float, float, float],
    *,
    city: str = "",
    region: str = "",
    country: str = "India",
    overpass_url: str = OVERPASS_URL,
    timeout: int = 60,
    max_retries: int = 3,
    retry_backoff_s: float = 2.0,
) -> list[Company]:
    """Fetch likely tech offices from OpenStreetMap/Overpass within a bbox."""
    query = build_overpass_query(bbox, timeout=timeout)
    response = None
    for attempt in range(max_retries + 1):
        response = requests.post(
            overpass_url,
            data={"data": query},
            headers={"User-Agent": "india-tech-company-finder/0.4"},
            timeout=timeout + 10,
        )
        if response.status_code != 429:
            break
        if attempt < max_retries:
            time.sleep(min(retry_backoff_s * (2 ** attempt), 60))
    assert response is not None
    response.raise_for_status()
    payload = response.json()
    return list(
        _iter_companies(
            payload.get("elements", []),
            city=city,
            region=region,
            country=country,
        )
    )
