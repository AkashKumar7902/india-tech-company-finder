from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from .careers import _detect_provider
from .models import Company


USER_AGENT = "india-tech-company-finder-jobs/0.1"
DEFAULT_LOCATION_KEYWORDS = ["bengaluru", "bangalore", "india", "remote", "hybrid"]
DEFAULT_TITLE_KEYWORDS = [
    "software",
    "sde",
    "backend",
    "back end",
    "frontend",
    "front end",
    "full stack",
    "engineer",
    "developer",
    "devops",
    "site reliability",
    "sre",
    "platform",
    "infrastructure",
    "cloud",
    "data engineer",
    "machine learning",
    "ai",
    "qa",
    "automation",
    "kubernetes",
    "java",
    "python",
    "golang",
    "go developer",
]


@dataclass(frozen=True)
class CareerSource:
    company: Company
    provider: str
    career_url: str
    api_url: str
    source_key: str


@dataclass
class PollResult:
    source: CareerSource
    success: bool
    jobs: list[dict[str, Any]]
    error: str = ""


class JobPollingError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _json_hash(payload: dict[str, Any]) -> str:
    important = {
        "title": payload.get("title"),
        "location": payload.get("location"),
        "department": payload.get("department"),
        "employment_type": payload.get("employment_type"),
        "workplace_type": payload.get("workplace_type"),
        "url": payload.get("url"),
    }
    return _stable_hash(json.dumps(important, ensure_ascii=False, sort_keys=True, default=str))


def _domain(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, dict):
        parts = []
        for key in ("name", "city", "region", "country", "location", "label"):
            if value.get(key):
                parts.append(str(value[key]))
        return ", ".join(parts) if parts else json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, list):
        return ", ".join(_clean_text(item) for item in value if _clean_text(item))
    return str(value)


def _timestamp_from_ms(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return _clean_text(value)
    # Lever uses milliseconds.
    if number > 10_000_000_000:
        number = number // 1000
    try:
        return datetime.fromtimestamp(number, timezone.utc).replace(microsecond=0).isoformat()
    except (OSError, OverflowError, ValueError):
        return str(value)


def _url_with_params(url: str, **params) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is not None:
            query[key] = str(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


def _get_json(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    method: str = "GET",
    json_payload: Optional[dict] = None,
    max_retries: int = 3,
    backoff_s: float = 2.0,
) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.8,*/*;q=0.5"}
    for attempt in range(max_retries + 1):
        response = session.request(method, url, headers=headers, json=json_payload, timeout=timeout, allow_redirects=True)
        if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries:
            time.sleep(min(backoff_s * (2 ** attempt), 60))
            continue
        response.raise_for_status()
        return response.json()
    raise JobPollingError(f"request failed: {url}")


def _get_text(session: requests.Session, url: str, *, timeout: int, max_retries: int = 2) -> tuple[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    for attempt in range(max_retries + 1):
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries:
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        return response.url, response.text[:2_000_000]
    raise JobPollingError(f"request failed: {url}")


def _provider_from_company(company: Company) -> tuple[str, str]:
    provider = (company.careers_provider or "").strip().lower()
    api_url = company.careers_api_url or ""
    if provider and (api_url or company.careers_url):
        return provider, api_url
    if company.careers_url:
        detected_provider, detected_api = _detect_provider(company.careers_url, "")
        return detected_provider or provider or "company_website", api_url or detected_api
    return provider, api_url


def _derive_workable_token(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "apply.workable.com" in parsed.netloc.lower() and parts:
        return parts[0]
    if parsed.netloc.endswith(".workable.com"):
        return parsed.netloc.split(".")[0]
    return ""


def _derive_recruitee_token(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.split(".")[0] if host.endswith(".recruitee.com") else ""


def career_sources_from_companies(companies: Iterable[Company]) -> list[CareerSource]:
    sources: list[CareerSource] = []
    seen = set()
    for company in companies:
        if not company.careers_url and not company.careers_api_url:
            continue
        provider, api_url = _provider_from_company(company)
        provider = provider or "company_website"
        career_url = company.careers_url or api_url
        source_key = _stable_hash("|".join([company.name, provider, api_url or career_url]))
        if source_key in seen:
            continue
        seen.add(source_key)
        sources.append(CareerSource(company=company, provider=provider, career_url=career_url, api_url=api_url, source_key=source_key))
    return sources


def normalize_job(raw: dict[str, Any], source: CareerSource) -> dict[str, Any]:
    external_id = _clean_text(raw.get("external_id") or raw.get("id") or raw.get("shortcode") or raw.get("req_id") or raw.get("url"))
    title = _clean_text(raw.get("title") or raw.get("text") or raw.get("name"))
    url = _clean_text(raw.get("url") or raw.get("absolute_url") or raw.get("hostedUrl") or raw.get("jobUrl") or raw.get("applyUrl"))
    if not external_id:
        external_id = _stable_hash("|".join([title, url, source.source_key]))[:24]

    job = {
        "job_key": _stable_hash("|".join([source.source_key, external_id or url or title])),
        "source_key": source.source_key,
        "company_name": source.company.name,
        "company_domain": _domain(source.company.website),
        "company_city": source.company.city,
        "source_provider": source.provider,
        "career_url": source.career_url,
        "api_url": source.api_url,
        "external_id": external_id,
        "title": title,
        "location": _clean_text(raw.get("location")),
        "department": _clean_text(raw.get("department")),
        "employment_type": _clean_text(raw.get("employment_type")),
        "workplace_type": _clean_text(raw.get("workplace_type")),
        "url": url,
        "posted_at": _clean_text(raw.get("posted_at")),
        "updated_at": _clean_text(raw.get("updated_at")),
        "status": "open",
    }
    job["content_hash"] = _json_hash(job)
    return job


def _greenhouse_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    api_url = source.api_url
    if not api_url:
        match = re.search(r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)", source.career_url)
        if match:
            api_url = f"https://boards-api.greenhouse.io/v1/boards/{match.group(1)}/jobs?content=true"
    if not api_url:
        return []
    data = _get_json(session, _url_with_params(api_url, content="true"), timeout=timeout)
    jobs = []
    for item in data.get("jobs", []) if isinstance(data, dict) else []:
        jobs.append(
            {
                "external_id": item.get("id"),
                "title": item.get("title"),
                "location": (item.get("location") or {}).get("name"),
                "department": _clean_text([dept.get("name") for dept in item.get("departments", []) if isinstance(dept, dict)]),
                "url": item.get("absolute_url"),
                "updated_at": item.get("updated_at"),
                "posted_at": item.get("first_published"),
            }
        )
    return jobs


def _lever_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    api_url = source.api_url
    if not api_url:
        match = re.search(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", source.career_url)
        if match:
            api_url = f"https://api.lever.co/v0/postings/{match.group(1)}?mode=json"
    if not api_url:
        return []

    jobs = []
    skip = 0
    for _ in range(max_pages):
        url = _url_with_params(api_url, mode="json", limit=100, skip=skip)
        data = _get_json(session, url, timeout=timeout)
        if not isinstance(data, list) or not data:
            break
        for item in data:
            categories = item.get("categories") or {}
            jobs.append(
                {
                    "external_id": item.get("id"),
                    "title": item.get("text"),
                    "location": categories.get("location"),
                    "department": categories.get("department"),
                    "employment_type": categories.get("commitment"),
                    "workplace_type": categories.get("team"),
                    "url": item.get("hostedUrl") or item.get("applyUrl"),
                    "posted_at": _timestamp_from_ms(item.get("createdAt")),
                    "updated_at": _timestamp_from_ms(item.get("createdAt")),
                }
            )
        skip += len(data)
        if len(data) < 100:
            break
    return jobs


def _ashby_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    api_url = source.api_url
    if not api_url:
        match = re.search(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", source.career_url)
        if match:
            api_url = f"https://api.ashbyhq.com/posting-api/job-board/{match.group(1)}"
    if not api_url:
        return []
    data = _get_json(session, _url_with_params(api_url, includeCompensation="true"), timeout=timeout)
    jobs = []
    for item in data.get("jobs", []) if isinstance(data, dict) else []:
        jobs.append(
            {
                "external_id": item.get("id"),
                "title": item.get("title"),
                "location": item.get("locationName") or item.get("location"),
                "department": item.get("department"),
                "employment_type": item.get("employmentType"),
                "url": item.get("jobUrl") or item.get("applyUrl"),
                "updated_at": item.get("updatedAt"),
                "posted_at": item.get("publishedAt"),
            }
        )
    return jobs


def _smartrecruiters_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    api_url = source.api_url
    if not api_url:
        match = re.search(r"(?:jobs|careers)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", source.career_url)
        if match:
            api_url = f"https://api.smartrecruiters.com/v1/companies/{match.group(1)}/postings"
    if not api_url:
        return []
    data = _get_json(session, _url_with_params(api_url, limit=100, destination="PUBLIC"), timeout=timeout)
    jobs = []
    for item in data.get("content", []) if isinstance(data, dict) else []:
        loc = item.get("location") or {}
        dept = item.get("department") or {}
        jobs.append(
            {
                "external_id": item.get("id"),
                "title": item.get("name"),
                "location": loc.get("city") or _clean_text(loc),
                "department": dept.get("label") or _clean_text(dept),
                "url": item.get("ref"),
                "updated_at": item.get("releasedDate"),
                "posted_at": item.get("releasedDate"),
            }
        )
    return jobs


def _workable_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    token = _derive_workable_token(source.career_url) or _derive_workable_token(source.api_url)
    api_url = source.api_url or (f"https://www.workable.com/api/accounts/{token}?details=true" if token else "")
    if not api_url:
        return []
    data = _get_json(session, api_url, timeout=timeout)
    raw_jobs = data.get("jobs", []) if isinstance(data, dict) else []
    jobs = []
    for item in raw_jobs:
        jobs.append(
            {
                "external_id": item.get("id") or item.get("shortcode"),
                "title": item.get("title"),
                "location": item.get("location"),
                "department": item.get("department"),
                "employment_type": item.get("type"),
                "url": item.get("url") or item.get("application_url"),
                "updated_at": item.get("published_on"),
                "posted_at": item.get("published_on"),
            }
        )
    return jobs


def _recruitee_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    token = _derive_recruitee_token(source.career_url) or _derive_recruitee_token(source.api_url)
    api_url = source.api_url or (f"https://{token}.recruitee.com/api/offers/" if token else "")
    if not api_url:
        return []
    data = _get_json(session, api_url, timeout=timeout)
    raw_jobs = data.get("offers", data if isinstance(data, list) else []) if isinstance(data, (dict, list)) else []
    jobs = []
    for item in raw_jobs:
        jobs.append(
            {
                "external_id": item.get("id"),
                "title": item.get("title"),
                "location": item.get("location"),
                "department": item.get("department"),
                "url": item.get("careers_url") or item.get("url"),
                "updated_at": item.get("updated_at"),
                "posted_at": item.get("created_at"),
            }
        )
    return jobs


def _workday_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    api_url = source.api_url
    if not api_url:
        provider, api_url = _detect_provider(source.career_url, "")
    if not api_url:
        return []
    jobs = []
    offset = 0
    for _ in range(max_pages):
        payload = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
        data = _get_json(session, api_url, timeout=timeout, method="POST", json_payload=payload)
        raw_jobs = data.get("jobPostings", []) if isinstance(data, dict) else []
        if not raw_jobs:
            break
        root = api_url.split("/wday/cxs/")[0] if "/wday/cxs/" in api_url else source.career_url
        for item in raw_jobs:
            external_path = item.get("externalPath") or item.get("url") or ""
            jobs.append(
                {
                    "external_id": item.get("bulletFields", [None])[0] or item.get("title") or external_path,
                    "title": item.get("title"),
                    "location": item.get("locationsText") or item.get("location"),
                    "url": urljoin(root, external_path),
                    "updated_at": item.get("postedOn"),
                    "posted_at": item.get("postedOn"),
                }
            )
        offset += len(raw_jobs)
        total = data.get("total") or data.get("totalResults") or 0
        if total and offset >= int(total):
            break
    return jobs


def _extract_jsonld_jobs(html: str, page_url: str) -> list[dict[str, Any]]:
    jobs = []
    scripts = re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html, flags=re.I | re.S)

    def iter_items(value):
        if isinstance(value, list):
            for item in value:
                yield from iter_items(item)
        elif isinstance(value, dict):
            yield value
            graph = value.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    yield from iter_items(item)

    for script in scripts:
        try:
            payload = json.loads(script.strip())
        except Exception:
            continue
        for item in iter_items(payload):
            typ = item.get("@type")
            is_job = typ == "JobPosting" or (isinstance(typ, list) and "JobPosting" in typ)
            if not is_job:
                continue
            jobs.append(
                {
                    "external_id": item.get("identifier") or item.get("url") or item.get("title"),
                    "title": item.get("title"),
                    "location": item.get("jobLocation"),
                    "employment_type": item.get("employmentType"),
                    "url": item.get("url") or page_url,
                    "posted_at": item.get("datePosted"),
                    "updated_at": item.get("validThrough"),
                }
            )
    return jobs


def _generic_jsonld_jobs(source: CareerSource, session: requests.Session, *, timeout: int, max_pages: int) -> list[dict[str, Any]]:
    if not source.career_url:
        return []
    final_url, html = _get_text(session, source.career_url, timeout=timeout)
    return _extract_jsonld_jobs(html, final_url)


PROVIDER_ADAPTERS = {
    "greenhouse": _greenhouse_jobs,
    "lever": _lever_jobs,
    "ashby": _ashby_jobs,
    "smartrecruiters": _smartrecruiters_jobs,
    "workable": _workable_jobs,
    "recruitee": _recruitee_jobs,
    "workday": _workday_jobs,
    "company_website": _generic_jsonld_jobs,
    "custom": _generic_jsonld_jobs,
}


def poll_source(source: CareerSource, *, timeout: int, max_pages: int) -> PollResult:
    session = requests.Session()
    adapter = PROVIDER_ADAPTERS.get(source.provider) or _generic_jsonld_jobs
    try:
        raw_jobs = adapter(source, session, timeout=timeout, max_pages=max_pages)
        jobs = [normalize_job(raw, source) for raw in raw_jobs if _clean_text(raw.get("title") or raw.get("text") or raw.get("name"))]
        return PollResult(source=source, success=True, jobs=jobs)
    except Exception as exc:  # noqa: BLE001 - watcher must continue source-by-source
        return PollResult(source=source, success=False, jobs=[], error=f"{type(exc).__name__}: {exc}")


def _matches_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered]


def annotate_match(job: dict[str, Any], *, location_keywords: list[str], title_keywords: list[str]) -> dict[str, Any]:
    title_text = _clean_text(job.get("title"))
    location_text = _clean_text(job.get("location"))
    combined = " ".join([title_text, location_text, _clean_text(job.get("company_city")), _clean_text(job.get("department"))])
    title_matches = _matches_keywords(title_text, title_keywords)
    location_matches = _matches_keywords(combined, location_keywords)
    job["matches_watch"] = bool(title_matches and location_matches)
    reasons = []
    if title_matches:
        reasons.append("title:" + ",".join(title_matches[:5]))
    if location_matches:
        reasons.append("location:" + ",".join(location_matches[:5]))
    job["match_reasons"] = reasons
    return job


def load_jobs(path: str | Path) -> list[dict[str, Any]]:
    jobs_path = Path(path)
    if not jobs_path.exists():
        return []
    try:
        payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def write_jobs(path: str | Path, jobs: list[dict[str, Any]]) -> Path:
    jobs_path = Path(path)
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return jobs_path


def merge_job_results(
    previous_jobs: list[dict[str, Any]],
    poll_results: list[PollResult],
    *,
    location_keywords: list[str],
    title_keywords: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    now = utc_now()
    previous_by_key = {job.get("job_key"): dict(job) for job in previous_jobs if job.get("job_key")}
    merged = dict(previous_by_key)
    new_jobs: list[dict[str, Any]] = []
    successful_source_keys = {result.source.source_key for result in poll_results if result.success}
    seen_by_source: dict[str, set[str]] = {source_key: set() for source_key in successful_source_keys}

    failures = []
    for result in poll_results:
        if not result.success:
            failures.append({"company": result.source.company.name, "provider": result.source.provider, "error": result.error})
            continue
        for job in result.jobs:
            key = job["job_key"]
            seen_by_source.setdefault(result.source.source_key, set()).add(key)
            annotate_match(job, location_keywords=location_keywords, title_keywords=title_keywords)
            if key in merged:
                first_seen = merged[key].get("first_seen_at") or now
                existing_status = merged[key].get("status")
                merged[key].update(job)
                merged[key]["first_seen_at"] = first_seen
                merged[key]["last_seen_at"] = now
                merged[key]["status"] = "open"
                if existing_status == "closed":
                    merged[key].pop("closed_at", None)
            else:
                job["first_seen_at"] = now
                job["last_seen_at"] = now
                job["status"] = "open"
                merged[key] = job
                new_jobs.append(dict(job))

    for key, job in list(merged.items()):
        source_key = job.get("source_key")
        if source_key in successful_source_keys and key not in seen_by_source.get(source_key, set()) and job.get("status") == "open":
            job["status"] = "closed"
            job["closed_at"] = now
            merged[key] = job

    all_jobs = sorted(merged.values(), key=lambda item: (item.get("status") != "open", item.get("company_name", "").lower(), item.get("title", "").lower()))
    new_matching = [job for job in new_jobs if job.get("matches_watch")]
    stats = {
        "previous_jobs": len(previous_jobs),
        "current_total_jobs": len(all_jobs),
        "new_jobs": len(new_jobs),
        "new_matching_jobs": len(new_matching),
        "successful_sources": len(successful_source_keys),
        "failed_sources": len(failures),
        "failures": failures[:20],
    }
    return all_jobs, new_jobs, new_matching, stats


def poll_job_sources(
    companies: Iterable[Company],
    *,
    previous_jobs: list[dict[str, Any]],
    batch_size: Optional[int],
    batch_index: int,
    timeout: int = 30,
    max_pages: int = 5,
    request_sleep_s: float = 0.5,
    location_keywords: Optional[list[str]] = None,
    title_keywords: Optional[list[str]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sources = career_sources_from_companies(companies)
    if batch_size and batch_size > 0 and batch_size < len(sources):
        batch_count = (len(sources) + batch_size - 1) // batch_size
        idx = batch_index % batch_count
        selected_sources = sources[idx * batch_size : (idx + 1) * batch_size]
        batch_label = f"{idx + 1}/{batch_count}"
    else:
        selected_sources = sources
        batch_label = "all"

    results: list[PollResult] = []
    for source in selected_sources:
        results.append(poll_source(source, timeout=timeout, max_pages=max_pages))
        if request_sleep_s > 0:
            time.sleep(request_sleep_s)

    all_jobs, new_jobs, new_matching, stats = merge_job_results(
        previous_jobs,
        results,
        location_keywords=location_keywords or DEFAULT_LOCATION_KEYWORDS,
        title_keywords=title_keywords or DEFAULT_TITLE_KEYWORDS,
    )
    stats.update(
        {
            "total_sources": len(sources),
            "selected_sources": len(selected_sources),
            "batch": batch_label,
        }
    )
    return all_jobs, new_jobs, new_matching, stats
