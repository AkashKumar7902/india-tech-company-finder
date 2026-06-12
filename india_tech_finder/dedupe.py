from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

from .geo import distance_m
from .models import Company


LEGAL_SUFFIX_RE = re.compile(
    r"\b(private limited|pvt\.?\s*ltd\.?|limited|ltd\.?|llp|inc\.?|corp\.?|corporation|opc)\b",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    value = name.lower().replace("&", " and ")
    value = LEGAL_SUFFIX_RE.sub(" ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_website(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.netloc or parsed.path).lower()
    if host.startswith("www."):
        host = host[4:]
    return host.rstrip("/")


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _same_source_id(a: Company, b: Company) -> bool:
    for source, source_id in a.source_ids.items():
        if source_id and b.source_ids.get(source) == source_id:
            return True
    return False


def _different_declared_city(a: Company, b: Company) -> bool:
    if not a.city or not b.city:
        return False
    if a.city.strip().lower() == b.city.strip().lower():
        return False
    dist = distance_m(a.lat, a.lng, b.lat, b.lng)
    # Overlapping city searches can return the same place; coordinates close
    # enough should still be allowed to merge.
    return dist is None or dist > 1200


def _is_duplicate(a: Company, b: Company) -> bool:
    if _same_source_id(a, b):
        return True

    if _different_declared_city(a, b):
        return False

    a_web = normalize_website(a.website)
    b_web = normalize_website(b.website)
    if a_web and b_web and a_web == b_web:
        return True

    a_name = normalize_name(a.name)
    b_name = normalize_name(b.name)
    sim = _similarity(a_name, b_name)
    dist = distance_m(a.lat, a.lng, b.lat, b.lng)

    if a_name and b_name and a_name == b_name:
        return dist is None or dist <= 800
    if sim >= 0.95:
        return dist is None or dist <= 800
    if sim >= 0.88 and dist is not None and dist <= 250:
        return True
    return False


def _union(left, right):
    seen = set()
    output = []
    for value in list(left or []) + list(right or []):
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _career_rank(confidence: str) -> int:
    return {"": 0, "none": 0, "low": 1, "medium": 2, "high": 3}.get((confidence or "").lower(), 0)


def _merge_careers(base: Company, incoming: Company) -> None:
    if _career_rank(incoming.careers_confidence) > _career_rank(base.careers_confidence):
        base.careers_url = incoming.careers_url
        base.careers_provider = incoming.careers_provider
        base.careers_api_url = incoming.careers_api_url
        base.careers_confidence = incoming.careers_confidence
        base.careers_last_checked = incoming.careers_last_checked
        base.careers_notes = incoming.careers_notes
        return
    if not base.careers_url and incoming.careers_url:
        base.careers_url = incoming.careers_url
    if not base.careers_provider and incoming.careers_provider:
        base.careers_provider = incoming.careers_provider
    if not base.careers_api_url and incoming.careers_api_url:
        base.careers_api_url = incoming.careers_api_url
    if not base.careers_confidence and incoming.careers_confidence:
        base.careers_confidence = incoming.careers_confidence
    if not base.careers_last_checked and incoming.careers_last_checked:
        base.careers_last_checked = incoming.careers_last_checked
    if not base.careers_notes and incoming.careers_notes:
        base.careers_notes = incoming.careers_notes


def merge_company(base: Company, incoming: Company) -> Company:
    # Prefer the richer display name/address, but keep coordinates already found.
    if len(incoming.name or "") > len(base.name or ""):
        base.name = incoming.name
    if not base.city and incoming.city:
        base.city = incoming.city
    if not base.region and incoming.region:
        base.region = incoming.region
    if not base.country and incoming.country:
        base.country = incoming.country
    if not base.address or len(incoming.address or "") > len(base.address or ""):
        base.address = incoming.address
    if base.lat is None and incoming.lat is not None:
        base.lat = incoming.lat
    if base.lng is None and incoming.lng is not None:
        base.lng = incoming.lng
    if not base.website and incoming.website:
        base.website = incoming.website
    if not base.phone and incoming.phone:
        base.phone = incoming.phone
    _merge_careers(base, incoming)

    base.categories = _union(base.categories, incoming.categories)
    base.sources = _union(base.sources, incoming.sources)
    base.source_ids.update({k: v for k, v in incoming.source_ids.items() if v})

    # Merge selected raw metadata without storing huge duplicate API payloads.
    base.raw["google_queries"] = _union(
        base.raw.get("google_queries", []), incoming.raw.get("google_queries", [])
    )
    base.raw["google_types"] = _union(
        base.raw.get("google_types", []), incoming.raw.get("google_types", [])
    )
    base.raw["google_search_points"] = _union(
        base.raw.get("google_search_points", []), incoming.raw.get("google_search_points", [])
    )
    base.raw["search_queries"] = _union(
        base.raw.get("search_queries", []), incoming.raw.get("search_queries", [])
    )
    if not base.raw.get("search_url") and incoming.raw.get("search_url"):
        base.raw["search_url"] = incoming.raw.get("search_url")
    if not base.raw.get("search_title") and incoming.raw.get("search_title"):
        base.raw["search_title"] = incoming.raw.get("search_title")
    if incoming.raw.get("careers") and (
        "careers" not in base.raw
        or _career_rank((incoming.raw.get("careers") or {}).get("confidence", ""))
        > _career_rank((base.raw.get("careers") or {}).get("confidence", ""))
    ):
        base.raw["careers"] = incoming.raw.get("careers")
    osm_tags = dict(base.raw.get("osm_tags", {}) or {})
    osm_tags.update(incoming.raw.get("osm_tags", {}) or {})
    if osm_tags:
        base.raw["osm_tags"] = osm_tags
    return base


def dedupe_companies(companies: list[Company]) -> list[Company]:
    merged: list[Company] = []
    for company in companies:
        for existing in merged:
            if _is_duplicate(existing, company):
                merge_company(existing, company)
                break
        else:
            merged.append(company)
    return merged
