from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from .models import Company


# Weighted keywords. These are intentionally broad because public directories often
# tag software offices only as "establishment" or "office".
TECH_KEYWORDS: List[Tuple[str, int]] = [
    ("software", 30),
    ("saas", 28),
    ("information technology", 28),
    ("it services", 28),
    ("technology", 24),
    ("technologies", 24),
    ("infotech", 24),
    ("app development", 24),
    ("mobile app", 22),
    ("web development", 22),
    ("artificial intelligence", 22),
    ("machine learning", 22),
    ("data analytics", 22),
    ("cloud", 18),
    ("cyber security", 18),
    ("cybersecurity", 18),
    ("fintech", 18),
    ("devops", 16),
    ("systems", 14),
    ("solutions", 12),
    ("digital", 12),
    ("labs", 10),
    ("computer", 10),
    ("startup", 10),
    ("office=it", 35),
    ("office=company", 12),
]

NEGATIVE_KEYWORDS: List[Tuple[str, int]] = [
    ("training institute", 35),
    ("institute", 22),
    ("academy", 22),
    ("school", 22),
    ("college", 22),
    ("hostel", 35),
    ("hotel", 35),
    ("restaurant", 35),
    ("cafe", 28),
    ("salon", 30),
    ("clinic", 30),
    ("hospital", 35),
    ("real estate", 24),
    ("pg", 24),
    ("paying guest", 24),
    ("computer repair", 22),
    ("mobile repair", 22),
    ("printing", 18),
]


def _haystack(company: Company) -> str:
    parts = [
        company.name,
        company.address,
        " ".join(company.categories),
        " ".join(company.sources),
    ]
    # Include source tags, but do not include entire raw API payloads.
    for key in ("osm_tags", "google_types", "google_places_new_types", "search_queries"):
        value = company.raw.get(key)
        if isinstance(value, dict):
            parts.extend(f"{k}={v}" for k, v in value.items())
        elif isinstance(value, list):
            parts.extend(str(v) for v in value)
    return " ".join(str(p) for p in parts if p).lower()


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = phrase.lower().strip()
    if "=" in phrase:
        return phrase in text
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None


def _matching_keywords(text: str, keywords: Iterable[Tuple[str, int]]) -> List[Tuple[str, int]]:
    return [(keyword, weight) for keyword, weight in keywords if _contains_phrase(text, keyword)]


def score_company(company: Company) -> Company:
    """Assign a 0-100 tech-likelihood score and confidence label."""
    text = _haystack(company)
    positives = _matching_keywords(text, TECH_KEYWORDS)
    negatives = _matching_keywords(text, NEGATIVE_KEYWORDS)

    score = sum(weight for _, weight in positives)

    # A Google Places result coming from a tech-oriented query is useful even if
    # Google only returns generic place types like "point_of_interest".
    google_queries = company.raw.get("google_queries", [])
    if any(_contains_phrase(str(query).lower(), kw) for query in google_queries for kw, _ in TECH_KEYWORDS):
        score += 16

    if company.website:
        score += 5
    if company.phone:
        score += 2
    google_new_queries = company.raw.get("google_places_new_queries", [])
    if any(_contains_phrase(str(query).lower(), kw) for query in google_new_queries for kw, _ in TECH_KEYWORDS):
        score += 16

    search_queries = company.raw.get("search_queries", [])
    if any(_contains_phrase(str(query).lower(), kw) for query in search_queries for kw, _ in TECH_KEYWORDS):
        score += 12

    if "google_places" in company.sources or "google_places_new" in company.sources:
        score += 5
    if "openstreetmap" in company.sources:
        score += 3
    if "web_search" in company.sources:
        score += 2
    if company.careers_url:
        score += 4

    score -= sum(weight for _, weight in negatives)
    score = max(0, min(100, score))

    if score >= 55:
        confidence = "high"
    elif score >= 35:
        confidence = "medium"
    elif score >= 20:
        confidence = "low"
    else:
        confidence = "review"

    matched = ", ".join(keyword for keyword, _ in positives) or "no strong tech keyword"
    negative_note = ""
    if negatives:
        negative_note = "; negative hints: " + ", ".join(keyword for keyword, _ in negatives)

    company.tech_score = float(score)
    company.confidence = confidence
    company.notes = f"matched: {matched}{negative_note}"
    return company
