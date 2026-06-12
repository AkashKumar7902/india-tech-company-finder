from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import requests

from .models import Company


USER_AGENT = "india-tech-company-finder-careers/0.1"
CAREERS_KEYWORDS = [
    "career",
    "careers",
    "jobs",
    "job openings",
    "openings",
    "join us",
    "join-us",
    "work with us",
    "work-with-us",
    "life at",
    "life-at",
    "hiring",
    "vacancies",
    "opportunities",
]
COMMON_CAREERS_PATHS = [
    "/careers",
    "/careers/",
    "/career",
    "/career/",
    "/jobs",
    "/jobs/",
    "/join-us",
    "/join-us/",
    "/work-with-us",
    "/work-with-us/",
    "/openings",
    "/openings/",
    "/current-openings",
    "/current-openings/",
    "/hiring",
    "/hiring/",
]


@dataclass
class CareersInfo:
    careers_url: str = ""
    provider: str = ""
    api_url: str = ""
    confidence: str = ""
    notes: str = ""
    checked_at: str = ""


@dataclass
class LinkCandidate:
    url: str
    text: str
    score: int


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag.lower() != "a":
            return
        attrs_dict = {str(key).lower(): value for key, value in attrs}
        href = attrs_dict.get("href")
        if href:
            self._current_href = str(href)
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            text = " ".join(part.strip() for part in self._current_text if part.strip())
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text = []


PROVIDER_PATTERNS = [
    ("greenhouse", [r"boards\.greenhouse\.io", r"job-boards\.greenhouse\.io", r"greenhouse\.io"]),
    ("lever", [r"jobs\.lever\.co", r"api\.lever\.co"]),
    ("ashby", [r"jobs\.ashbyhq\.com", r"api\.ashbyhq\.com", r"ashbyhq"]),
    ("workday", [r"myworkdayjobs\.com", r"workdayjobs\.com", r"/wday/cxs/"]),
    ("smartrecruiters", [r"jobs\.smartrecruiters\.com", r"api\.smartrecruiters\.com"]),
    ("workable", [r"apply\.workable\.com", r"workable\.com"]),
    ("recruitee", [r"recruitee\.com"]),
    ("bamboohr", [r"bamboohr\.com/careers", r"bamboohr"]),
    ("freshteam", [r"freshteam\.com/jobs", r"freshteam"]),
    ("teamtailor", [r"teamtailor\.com", r"teamtailor"]),
    ("breezy", [r"breezy\.hr"]),
    ("icims", [r"icims\.com"]),
    ("successfactors", [r"successfactors\.com"]),
    ("oracle_recruiting", [r"oraclecloud\.com/hcm", r"fa-ext\.oraclecloud\.com"]),
    ("taleo", [r"taleo\.net"]),
    ("zoho_recruit", [r"zohorecruit", r"zoho\.com/recruit"]),
    ("darwinbox", [r"darwinbox\.com", r"darwinbox"]),
    ("eightfold", [r"eightfold\.ai"]),
    ("trakstar_hire", [r"hire\.trakstar\.com"]),
    ("jazzhr", [r"applytojob\.com", r"jazz\.co"]),
]


API_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+(?:api|jobs|job|careers|career|postings|positions|openings)[^\s\"'<>\\]*",
    re.IGNORECASE,
)
RELATIVE_API_RE = re.compile(
    r"[\"']((?:/|\.\./)[^\"']*(?:api|jobs|job|careers|career|postings|positions|openings)[^\"']*)[\"']",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_website(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path or "/", "", "", ""))


def _strip_www(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _same_site(base_url: str, candidate_url: str) -> bool:
    base_host = _strip_www(urlparse(base_url).netloc)
    candidate_host = _strip_www(urlparse(candidate_url).netloc)
    return bool(base_host and candidate_host and (base_host == candidate_host or candidate_host.endswith(f".{base_host}")))


def _safe_get(session: requests.Session, url: str, *, timeout: int) -> tuple[str, str]:
    response = session.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "text" not in content_type and "html" not in content_type and "xml" not in content_type:
        return response.url, ""
    return response.url, response.text[:1_500_000]


def _html_links(html: str, base_url: str) -> list[LinkCandidate]:
    parser = LinkExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass

    candidates: list[LinkCandidate] = []
    seen = set()
    for href, text in parser.links:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue

        haystack = f"{url} {text}".lower()
        score = 0
        for keyword in CAREERS_KEYWORDS:
            if keyword in haystack:
                score += 10
        if _detect_provider(url, "")[0]:
            score += 40
        if not _same_site(base_url, url):
            score += 5
        if score <= 0:
            continue
        normalized = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", parsed.query, ""))
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(LinkCandidate(url=normalized, text=text, score=score))

    candidates.sort(key=lambda candidate: (-candidate.score, len(candidate.url)))
    return candidates


def _common_path_candidates(base_url: str) -> list[LinkCandidate]:
    parsed = urlparse(base_url)
    root = urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
    return [LinkCandidate(url=urljoin(root, path), text=path.strip("/"), score=8) for path in COMMON_CAREERS_PATHS]


def _career_like_url(url: str) -> bool:
    lowered = url.lower()
    return any(keyword.replace(" ", "-") in lowered or keyword.replace(" ", "") in lowered for keyword in CAREERS_KEYWORDS)


def _loc_urls_from_xml(xml_text: str) -> list[str]:
    urls: list[str] = []
    try:
        root = ElementTree.fromstring(xml_text.encode("utf-8"))
        for element in root.iter():
            if element.tag.lower().endswith("loc") and element.text:
                urls.append(element.text.strip())
    except Exception:
        urls.extend(re.findall(r"<loc>\s*([^<]+)\s*</loc>", xml_text, flags=re.IGNORECASE))
    return urls


def _sitemap_candidates(
    session: requests.Session,
    base_url: str,
    *,
    timeout: int,
    max_sitemaps: int = 3,
    max_urls: int = 20,
) -> list[LinkCandidate]:
    parsed = urlparse(base_url)
    root = urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
    sitemap_urls = [urljoin(root, "/sitemap.xml"), urljoin(root, "/sitemap_index.xml")]

    try:
        robots_url, robots_text = _safe_get(session, urljoin(root, "/robots.txt"), timeout=timeout)
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_urls.append(line.split(":", 1)[1].strip())
    except Exception:
        pass

    candidates: list[LinkCandidate] = []
    seen_sitemaps = set()
    seen_candidates = set()
    queue = []
    for sitemap_url in sitemap_urls:
        if sitemap_url and sitemap_url not in seen_sitemaps:
            seen_sitemaps.add(sitemap_url)
            queue.append(sitemap_url)

    while queue and len(seen_sitemaps) <= max_sitemaps + 2 and len(candidates) < max_urls:
        sitemap_url = queue.pop(0)
        try:
            final_url, xml_text = _safe_get(session, sitemap_url, timeout=timeout)
        except Exception:
            continue
        for loc_url in _loc_urls_from_xml(xml_text):
            if not loc_url:
                continue
            if loc_url.lower().endswith(".xml") and len(seen_sitemaps) < max_sitemaps:
                if loc_url not in seen_sitemaps:
                    seen_sitemaps.add(loc_url)
                    queue.append(loc_url)
                continue
            if _career_like_url(loc_url) and loc_url not in seen_candidates:
                seen_candidates.add(loc_url)
                candidates.append(LinkCandidate(url=loc_url, text="sitemap", score=35))
                if len(candidates) >= max_urls:
                    break
    return candidates


def _extract_api_urls(html: str, page_url: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    for match in API_URL_RE.findall(html or ""):
        clean = match.rstrip(".,);]")
        if clean not in seen:
            seen.add(clean)
            urls.append(clean)
    for match in RELATIVE_API_RE.findall(html or ""):
        clean = urljoin(page_url, match).rstrip(".,);]")
        if clean not in seen:
            seen.add(clean)
            urls.append(clean)
    return urls[:10]


def _detect_provider(url: str, html: str) -> tuple[str, str]:
    haystack = f"{url}\n{html[:500_000]}".lower()
    for provider, patterns in PROVIDER_PATTERNS:
        if any(re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns):
            return provider, _provider_api_url(provider, url, html)
    return "", ""


def _first_path_segment(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else ""


def _provider_api_url(provider: str, url: str, html: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path_first = _first_path_segment(url)

    if provider == "greenhouse":
        token = ""
        match = re.search(r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)", url)
        if not match:
            match = re.search(r"for=([a-zA-Z0-9_-]+)", html)
        if match:
            token = match.group(1)
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs" if token else ""

    if provider == "lever":
        match = re.search(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", url)
        org = match.group(1) if match else path_first
        return f"https://api.lever.co/v0/postings/{org}?mode=json" if org else ""

    if provider == "ashby":
        match = re.search(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", url)
        org = match.group(1) if match else path_first
        return f"https://api.ashbyhq.com/posting-api/job-board/{org}" if org else ""

    if provider == "smartrecruiters":
        match = re.search(r"jobs\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", url)
        org = match.group(1) if match else path_first
        return f"https://api.smartrecruiters.com/v1/companies/{org}/postings" if org else ""

    if provider == "workday":
        # Example: https://company.wd1.myworkdayjobs.com/en-US/Careers
        site_match = re.search(r"/(?:en-US|en|en_GB|en-IN)/([^/?#]+)", parsed.path)
        site = site_match.group(1) if site_match else path_first
        tenant = host.split(".")[0]
        return f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs" if tenant and site else ""

    extracted = _extract_api_urls(html, url)
    if extracted:
        return extracted[0]
    return ""


def _best_api_url(provider_api_url: str, extracted_api_urls: list[str]) -> str:
    if provider_api_url:
        return provider_api_url
    for url in extracted_api_urls:
        lowered = url.lower()
        if any(word in lowered for word in ("jobs", "careers", "openings", "postings", "positions")):
            return url
    return extracted_api_urls[0] if extracted_api_urls else ""


def _candidate_urls_for_website(session: requests.Session, website: str, *, timeout: int, max_pages: int) -> tuple[str, list[LinkCandidate], str]:
    base_url = normalize_website(website)
    if not base_url:
        return "", [], "invalid website"

    notes = []
    candidates: list[LinkCandidate] = []
    homepage_html = ""
    try:
        final_url, homepage_html = _safe_get(session, base_url, timeout=timeout)
        base_url = final_url or base_url
        candidates.extend(_html_links(homepage_html, base_url))
    except Exception as exc:  # noqa: BLE001 - best-effort enrichment
        notes.append(f"homepage fetch failed: {type(exc).__name__}")

    # If homepage itself is an ATS/careers page, evaluate it first.
    if _detect_provider(base_url, homepage_html)[0] or any(keyword in base_url.lower() for keyword in CAREERS_KEYWORDS):
        candidates.insert(0, LinkCandidate(base_url, "homepage", 100))

    candidates.extend(_sitemap_candidates(session, base_url, timeout=timeout, max_urls=max_pages * 2))
    candidates.extend(_common_path_candidates(base_url))

    deduped: list[LinkCandidate] = []
    seen = set()
    for candidate in sorted(candidates, key=lambda item: (-item.score, len(item.url))):
        if candidate.url not in seen:
            seen.add(candidate.url)
            deduped.append(candidate)
    return base_url, deduped[: max(max_pages, 1)], "; ".join(notes)


def find_careers_info(
    website: str,
    *,
    timeout: int = 10,
    max_pages: int = 5,
) -> CareersInfo:
    checked_at = _utc_now()
    if not website:
        return CareersInfo(checked_at=checked_at, confidence="none", notes="no website")

    session = requests.Session()
    base_url, candidates, note = _candidate_urls_for_website(session, website, timeout=timeout, max_pages=max_pages)
    if not base_url:
        return CareersInfo(checked_at=checked_at, confidence="none", notes=note or "invalid website")

    best: Optional[CareersInfo] = None
    failures = []
    for candidate in candidates:
        html = ""
        page_url = candidate.url
        try:
            page_url, html = _safe_get(session, candidate.url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            failures.append(f"{candidate.url}: {type(exc).__name__}")
            continue

        provider, provider_api_url = _detect_provider(page_url, html)
        extracted_api_urls = _extract_api_urls(html, page_url)
        api_url = _best_api_url(provider_api_url, extracted_api_urls)
        text = f"{page_url} {candidate.text} {html[:25_000]}".lower()
        has_career_signal = any(keyword in text for keyword in CAREERS_KEYWORDS)

        if provider:
            return CareersInfo(
                careers_url=page_url,
                provider=provider,
                api_url=api_url,
                confidence="high",
                checked_at=checked_at,
                notes="detected ATS/provider" + (f"; {note}" if note else ""),
            )

        if has_career_signal and not best:
            best = CareersInfo(
                careers_url=page_url,
                provider="company_website",
                api_url=api_url,
                confidence="medium" if api_url else "low",
                checked_at=checked_at,
                notes="careers page found on company website" + (f"; {note}" if note else ""),
            )

    if best:
        return best

    failure_note = "; ".join(failures[:3])
    notes = "; ".join(part for part in [note, failure_note, "no careers page detected"] if part)
    return CareersInfo(checked_at=checked_at, confidence="none", notes=notes)


def apply_careers_info(company: Company, info: CareersInfo) -> Company:
    company.careers_url = info.careers_url
    company.careers_provider = info.provider
    company.careers_api_url = info.api_url
    company.careers_confidence = info.confidence
    company.careers_last_checked = info.checked_at
    company.careers_notes = info.notes
    company.raw["careers"] = {
        "url": info.careers_url,
        "provider": info.provider,
        "api_url": info.api_url,
        "confidence": info.confidence,
        "checked_at": info.checked_at,
        "notes": info.notes,
    }
    return company


def enrich_companies_with_careers(
    companies: Iterable[Company],
    *,
    timeout: int = 10,
    max_pages: int = 5,
    request_sleep_s: float = 0.5,
) -> int:
    count = 0
    for company in companies:
        if not company.website:
            continue
        info = find_careers_info(company.website, timeout=timeout, max_pages=max_pages)
        apply_careers_info(company, info)
        count += 1
        if request_sleep_s > 0:
            time.sleep(request_sleep_s)
    return count
