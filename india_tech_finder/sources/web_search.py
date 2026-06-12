from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse, urlunparse

import requests

from ..careers import _detect_provider
from ..models import Company


BING_SEARCH_URL = "https://api.bing.microsoft.com/v7.0/search"
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"

SEARCH_QUERY_TEMPLATES = [
    '"software company" "{location}"',
    '"IT company" "{location}"',
    '"technology company" "{location}"',
    '"SaaS company" "{location}"',
    '"startup" "{location}"',
    '"software development company" "{location}"',
    'site:linkedin.com/company "{location}" software',
    'site:jobs.lever.co "{location}" software engineer',
    'site:boards.greenhouse.io "{location}" software engineer',
    'site:jobs.ashbyhq.com "{location}" software engineer',
    'site:jobs.smartrecruiters.com "{location}" software engineer',
    'site:wellfound.com/company "{location}" software',
    'site:cutshort.io/company "{location}" software',
]

JOB_OR_CAREERS_HOST_HINTS = (
    "jobs.lever.co",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.ashbyhq.com",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "jobs.smartrecruiters.com",
    "apply.workable.com",
    "recruitee.com",
    "freshteam.com",
    "bamboohr.com",
    "teamtailor.com",
    "breezy.hr",
    "icims.com",
    "successfactors.com",
    "taleo.net",
    "zohorecruit",
    "wellfound.com",
    "angel.co",
    "cutshort.io",
)

CAREERS_WORD_RE = re.compile(r"\b(careers?|jobs?|job openings?|open positions?|hiring|join us|work with us)\b", re.I)
SEPARATOR_RE = re.compile(r"\s+(?:\||-|–|—|:|·)\s+")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


def render_web_queries(
    templates: Iterable[str],
    *,
    location: str,
    city: str = "",
    state: str = "",
    country: str = "India",
) -> list[str]:
    rendered: list[str] = []
    seen = set()
    values = {
        "location": location,
        "city": city,
        "state": state,
        "country": country,
    }
    for template in templates:
        query = template.strip()
        if not query:
            continue
        query = query.format(**values) if "{" in query and "}" in query else f"{query} {location}"
        query = " ".join(query.split())
        key = query.lower()
        if key not in seen:
            seen.add(key)
            rendered.append(query)
    return rendered


def _slug_to_name(slug: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", " ", slug).strip()
    small_words = {"ai", "io", "it", "hr", "crm", "erp", "saas", "api"}
    parts = []
    for part in slug.split():
        lower = part.lower()
        parts.append(lower.upper() if lower in small_words else lower.capitalize())
    return " ".join(parts)


def _root_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def _path_segment(url: str, index: int = 0) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[index] if len(parts) > index else ""


def _name_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "linkedin.com" in host and "/company/" in parsed.path:
        return _slug_to_name(_path_segment(url, 1))
    if "jobs.lever.co" in host or "boards.greenhouse.io" in host or "jobs.ashbyhq.com" in host:
        return _slug_to_name(_path_segment(url, 0))
    if "jobs.smartrecruiters.com" in host:
        return _slug_to_name(_path_segment(url, 0))
    if "workable.com" in host or "recruitee.com" in host or "teamtailor.com" in host:
        segment = _path_segment(url, 0) or host.split(".")[0]
        return _slug_to_name(segment)
    if "freshteam.com" in host or "bamboohr.com" in host:
        return _slug_to_name(host.split(".")[0])
    domain = host[4:] if host.startswith("www.") else host
    return _slug_to_name(domain.split(".")[0])


def _name_from_title(title: str, url: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    if not title:
        return _name_from_url(url)

    match = re.search(r"careers?\s+(?:at|with)\s+(.+)$", title, re.I)
    if match:
        title = match.group(1)

    parts = [part.strip() for part in SEPARATOR_RE.split(title) if part.strip()]
    if parts:
        # Prefer the first segment that is not just a generic job/careers label.
        for part in parts:
            if not CAREERS_WORD_RE.fullmatch(part):
                title = part
                break

    title = re.sub(r"\b(official website|linkedin|wellfound|angellist|cutshort|naukri|indeed)\b", "", title, flags=re.I)
    title = CAREERS_WORD_RE.sub("", title)
    title = re.sub(r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|chennai|gurugram|noida|delhi)\b", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -|:·,")
    return title or _name_from_url(url)


def _is_job_or_careers_url(url: str, title: str = "", snippet: str = "") -> bool:
    lowered = f"{url} {title} {snippet}".lower()
    host = urlparse(url).netloc.lower()
    return any(hint in host for hint in JOB_OR_CAREERS_HOST_HINTS) or bool(CAREERS_WORD_RE.search(lowered))


def _source_id(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", parsed.query, ""))


def company_from_search_result(
    result: SearchResult,
    *,
    query: str,
    city: str,
    region: str,
    country: str = "India",
) -> Optional[Company]:
    if not result.url:
        return None
    parsed = urlparse(result.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    careers_url = result.url if _is_job_or_careers_url(result.url, result.title, result.snippet) else ""
    provider, api_url = _detect_provider(result.url, "") if careers_url else ("", "")
    if careers_url and not provider:
        provider = "company_website" if parsed.netloc else ""

    website = "" if provider and provider != "company_website" else _root_url(result.url)
    if "linkedin.com" in parsed.netloc.lower() or any(host in parsed.netloc.lower() for host in JOB_OR_CAREERS_HOST_HINTS):
        website = ""

    name = _name_from_url(result.url) if provider and provider != "company_website" else _name_from_title(result.title, result.url)
    if not name or len(name) < 2:
        return None

    raw = {
        "search_queries": [query],
        "search_title": result.title,
        "search_snippet": result.snippet,
        "search_url": result.url,
    }
    if careers_url:
        raw["careers"] = {
            "url": careers_url,
            "provider": provider,
            "api_url": api_url,
            "confidence": "high" if api_url else "medium",
            "checked_at": "",
            "notes": "found from web search result",
        }

    return Company(
        name=name,
        city=city,
        region=region,
        country=country,
        website=website,
        careers_url=careers_url,
        careers_provider=provider,
        careers_api_url=api_url,
        careers_confidence="high" if api_url else ("medium" if careers_url else ""),
        careers_notes="found from web search result" if careers_url else "",
        categories=["web_search"],
        sources=["web_search"],
        source_ids={"web_search": _source_id(result.url)},
        raw=raw,
    )


def _fetch_bing_query(
    session: requests.Session,
    *,
    api_key: str,
    endpoint: str,
    query: str,
    max_results: int,
    timeout: int,
) -> list[SearchResult]:
    response = session.get(
        endpoint,
        headers={"Ocp-Apim-Subscription-Key": api_key, "User-Agent": "india-tech-company-finder/0.7"},
        params={"q": query, "mkt": "en-IN", "count": min(max_results, 50), "responseFilter": "Webpages"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    values = payload.get("webPages", {}).get("value", [])
    return [SearchResult(title=item.get("name", ""), url=item.get("url", ""), snippet=item.get("snippet", "")) for item in values]


def _fetch_serpapi_query(
    session: requests.Session,
    *,
    api_key: str,
    endpoint: str,
    query: str,
    max_results: int,
    timeout: int,
) -> list[SearchResult]:
    response = session.get(
        endpoint,
        headers={"User-Agent": "india-tech-company-finder/0.7"},
        params={"engine": "google", "q": query, "api_key": api_key, "google_domain": "google.co.in", "gl": "in", "num": min(max_results, 20)},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    values = payload.get("organic_results", [])
    return [SearchResult(title=item.get("title", ""), url=item.get("link", ""), snippet=item.get("snippet", "")) for item in values]


def fetch_web_search(
    *,
    provider: str,
    bing_api_key: str = "",
    serpapi_api_key: str = "",
    bing_endpoint: str = BING_SEARCH_URL,
    serpapi_endpoint: str = SERPAPI_SEARCH_URL,
    locations: Iterable[str],
    city: str,
    region: str,
    country: str = "India",
    query_templates: Iterable[str] = SEARCH_QUERY_TEMPLATES,
    max_results: int = 10,
    timeout: int = 30,
    request_sleep_s: float = 0.5,
) -> list[Company]:
    """Find company candidates from official search APIs.

    Supported providers: bing, serpapi. This does not scrape search pages.
    """
    provider = provider.lower()
    if provider == "auto":
        provider = "bing" if bing_api_key else "serpapi" if serpapi_api_key else ""
    if provider == "bing" and not bing_api_key:
        raise ValueError("Bing search requires BING_SEARCH_API_KEY")
    if provider == "serpapi" and not serpapi_api_key:
        raise ValueError("SerpAPI search requires SERPAPI_API_KEY")
    if provider not in {"bing", "serpapi"}:
        raise ValueError("search provider must be bing, serpapi, or auto")

    session = requests.Session()
    companies: list[Company] = []
    seen_urls = set()
    for location in locations:
        queries = render_web_queries(query_templates, location=location, city=city, state=region, country=country)
        for query in queries:
            if provider == "bing":
                results = _fetch_bing_query(
                    session,
                    api_key=bing_api_key,
                    endpoint=bing_endpoint,
                    query=query,
                    max_results=max_results,
                    timeout=timeout,
                )
            else:
                results = _fetch_serpapi_query(
                    session,
                    api_key=serpapi_api_key,
                    endpoint=serpapi_endpoint,
                    query=query,
                    max_results=max_results,
                    timeout=timeout,
                )
            for result in results:
                key = _source_id(result.url)
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                company = company_from_search_result(result, query=query, city=city, region=region, country=country)
                if company:
                    companies.append(company)
            if request_sleep_s > 0:
                time.sleep(request_sleep_s)
    return companies
