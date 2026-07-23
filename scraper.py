#!/usr/bin/env python3
"""Deterministic job pre-filter: fetch → title match → title exclusion → dump candidates.

No LLM calls. Use this branch to inspect what survives config.json filters and
iterate on more deterministic rules before scoring with Gemini.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional until Meta is fetched
    curl_requests = None

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen_jobs.json"
CANDIDATES_PATH = ROOT / "candidates.json"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_seen(path: Path = SEEN_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def normalize_job(
    *,
    job_id: str,
    title: str,
    company: str,
    url: str,
    location: str = "",
    requirements: str = "",
) -> dict[str, str]:
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "url": url,
        "location": location or "",
        "requirements": requirements or "",
    }


def fetch_greenhouse(board_token: str, company: str | None = None) -> list[dict[str, str]]:
    """Fetch jobs from Greenhouse public boards API."""
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    try:
        resp = requests.get(api_url, params={"content": "true"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[greenhouse:{board_token}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    jobs_raw = payload.get("jobs") or []
    company_name = company or board_token
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        job_id = str(item.get("id") or item.get("absolute_url") or "")
        if not job_id:
            continue
        location = ""
        loc = item.get("location")
        if isinstance(loc, dict):
            location = loc.get("name") or ""
        elif isinstance(loc, str):
            location = loc

        # Greenhouse sometimes embeds HTML content; keep as requirements text.
        requirements = (item.get("content") or item.get("description") or "").strip()
        jobs.append(
            normalize_job(
                job_id=f"greenhouse:{board_token}:{job_id}",
                title=(item.get("title") or "").strip() or "Untitled",
                company=company_name,
                url=(item.get("absolute_url") or "").strip(),
                location=location,
                requirements=requirements,
            )
        )
    print(f"[greenhouse:{board_token}] fetched {len(jobs)} jobs")
    return jobs


def fetch_ashby(board_name: str, company: str | None = None) -> list[dict[str, str]]:
    """Fetch jobs from Ashby public job posting API."""
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"
    try:
        resp = requests.get(api_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[ashby:{board_name}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    jobs_raw = payload.get("jobs") or []
    company_name = company or board_name
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        if item.get("isListed") is False:
            continue
        job_id = str(item.get("id") or item.get("jobUrl") or "")
        if not job_id:
            continue

        location = (item.get("location") or "").strip()
        secondary = item.get("secondaryLocations") or []
        if isinstance(secondary, list) and secondary:
            extras = [
                (s.get("location") if isinstance(s, dict) else str(s) or "").strip()
                for s in secondary
            ]
            extras = [e for e in extras if e]
            if extras:
                location = ", ".join([location, *extras] if location else extras)

        if item.get("isRemote") or (item.get("workplaceType") or "").lower() == "remote":
            if location and "remote" not in location.lower():
                location = f"{location} (Remote)"
            elif not location:
                location = "Remote"

        requirements = (item.get("descriptionPlain") or item.get("descriptionHtml") or "").strip()
        jobs.append(
            normalize_job(
                job_id=f"ashby:{board_name}:{job_id}",
                title=(item.get("title") or "").strip() or "Untitled",
                company=company_name,
                url=(item.get("jobUrl") or item.get("applyUrl") or "").strip(),
                location=location,
                requirements=requirements,
            )
        )
    print(f"[ashby:{board_name}] fetched {len(jobs)} jobs")
    return jobs


def _lever_requirements(item: dict[str, Any]) -> str:
    """Combine Lever plain description + section lists into one requirements blob."""
    parts: list[str] = []
    plain = (item.get("descriptionPlain") or item.get("description") or "").strip()
    if plain:
        parts.append(plain)

    for section in item.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = (section.get("text") or "").strip()
        content = (section.get("content") or "").strip()
        if heading and content:
            parts.append(f"{heading}\n{content}")
        elif content:
            parts.append(content)

    additional = (item.get("additionalPlain") or item.get("additional") or "").strip()
    if additional:
        parts.append(additional)
    return "\n\n".join(parts).strip()


def fetch_lever(site: str, company: str | None = None) -> list[dict[str, str]]:
    """Fetch jobs from Lever public postings API."""
    api_url = f"https://api.lever.co/v0/postings/{site}"
    try:
        resp = requests.get(api_url, params={"mode": "json"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[lever:{site}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    jobs_raw = payload if isinstance(payload, list) else (payload.get("data") or [])
    company_name = company or site
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or item.get("hostedUrl") or "")
        if not job_id:
            continue

        categories = item.get("categories") or {}
        location = ""
        if isinstance(categories, dict):
            location = (categories.get("location") or "").strip()
            all_locs = categories.get("allLocations") or []
            if isinstance(all_locs, list) and all_locs:
                joined = ", ".join(str(x) for x in all_locs if x)
                if joined:
                    location = joined

        workplace = (item.get("workplaceType") or "").strip()
        if workplace and workplace.lower() == "remote":
            if location and "remote" not in location.lower():
                location = f"{location} (Remote)"
            elif not location:
                location = "Remote"

        jobs.append(
            normalize_job(
                job_id=f"lever:{site}:{job_id}",
                title=(item.get("text") or "").strip() or "Untitled",
                company=company_name,
                url=(item.get("hostedUrl") or item.get("applyUrl") or "").strip(),
                location=location,
                requirements=_lever_requirements(item),
            )
        )
    print(f"[lever:{site}] fetched {len(jobs)} jobs")
    return jobs


def fetch_eightfold(
    domain: str,
    host: str,
    company: str | None = None,
    *,
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch jobs from an Eightfold public careers API (e.g. Netflix).

    List endpoint returns title/location/url; descriptions are empty unless
    each position is fetched individually (skipped here — filters are title-only).
    """
    api_url = f"https://{host}/api/apply/v2/jobs"
    company_name = company or domain
    jobs: list[dict[str, str]] = []
    start = 0
    total: int | None = None

    while True:
        try:
            resp = requests.get(
                api_url,
                params={"domain": domain, "start": start, "num": page_size},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[eightfold:{domain}] fetch failed at start={start}: {exc}", file=sys.stderr)
            break

        payload = resp.json()
        if total is None:
            raw_count = payload.get("count")
            total = int(raw_count) if raw_count is not None else None

        positions = payload.get("positions") or []
        if not positions:
            break

        for item in positions:
            if not isinstance(item, dict) or item.get("isPrivate"):
                continue
            job_id = str(item.get("id") or "")
            if not job_id:
                continue

            locs = item.get("locations") or []
            if isinstance(locs, list) and locs:
                location = ", ".join(str(x) for x in locs if x)
            else:
                location = (item.get("location") or "").strip()

            url = (item.get("canonicalPositionUrl") or "").strip()
            if not url:
                url = f"https://{host}/careers/job/{job_id}"

            jobs.append(
                normalize_job(
                    job_id=f"eightfold:{domain}:{job_id}",
                    title=(item.get("name") or item.get("posting_name") or "").strip()
                    or "Untitled",
                    company=company_name,
                    url=url,
                    location=location,
                    requirements=(item.get("job_description") or "").strip(),
                )
            )

        start += len(positions)
        if total is not None and start >= total:
            break
        # Eightfold caps page size (~10); stop if a short page arrives early.
        if len(positions) < page_size:
            break
        if start > 10_000:
            print(f"[eightfold:{domain}] abort: pagination exceeded 10000", file=sys.stderr)
            break

    print(f"[eightfold:{domain}] fetched {len(jobs)} jobs")
    return jobs


_GOOGLE_CAREERS_URL = "https://www.google.com/about/careers/applications/jobs/results/"


def _google_html_blob(field: Any) -> str:
    """Pull HTML text from Google careers nested [null, html] fields."""
    if isinstance(field, list) and len(field) > 1 and field[1]:
        return str(field[1]).strip()
    if isinstance(field, str):
        return field.strip()
    return ""


def _google_locations(field: Any) -> str:
    """Join location display names from Google careers location tuples."""
    if not isinstance(field, list):
        return ""
    names: list[str] = []
    for loc in field:
        if isinstance(loc, list) and loc and loc[0]:
            names.append(str(loc[0]).strip())
        elif isinstance(loc, str) and loc.strip():
            names.append(loc.strip())
    return ", ".join(names)


def _parse_google_careers_payload(html: str) -> list[Any] | None:
    """Extract the ds:1 job list payload from Google Careers HTML."""
    marker = "AF_initDataCallback({key: 'ds:1'"
    start = html.find(marker)
    if start < 0:
        return None
    data_idx = html.find("data:", start)
    if data_idx < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(html, data_idx + len("data:"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, list) else None


def fetch_google(
    company_filter: str | None = "Google",
    *,
    display_name: str | None = None,
    page_size: int = 20,
) -> list[dict[str, str]]:
    """Fetch jobs from Google Careers (embedded AF_initDataCallback JSON).

    Google has no public jobs API; listings are embedded in the careers HTML.
    Paginate with ``page`` (1-indexed); ``pageSize`` is capped server-side at ~20.
    """
    company_name = display_name or company_filter or "Google"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    jobs: list[dict[str, str]] = []
    page = 1
    total: int | None = None
    max_pages = 500
    consecutive_failures = 0

    while page <= max_pages:
        params: dict[str, Any] = {"pageSize": page_size, "page": page}
        if company_filter:
            params["company"] = company_filter

        payload: list[Any] | None = None
        for attempt in range(3):
            try:
                resp = requests.get(
                    _GOOGLE_CAREERS_URL, params=params, headers=headers, timeout=45
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                print(
                    f"[google] fetch failed at page={page} attempt={attempt + 1}: {exc}",
                    file=sys.stderr,
                )
                consecutive_failures += 1
                continue

            payload = _parse_google_careers_payload(resp.text)
            if payload is not None:
                consecutive_failures = 0
                break
            print(
                f"[google] no job payload at page={page} attempt={attempt + 1}",
                file=sys.stderr,
            )
            consecutive_failures += 1

        if payload is None:
            if consecutive_failures >= 3:
                print(f"[google] aborting after failures at page={page}", file=sys.stderr)
                break
            page += 1
            continue

        if total is None and len(payload) >= 3 and payload[2] is not None:
            try:
                total = int(payload[2])
            except (TypeError, ValueError):
                total = None

        positions = payload[0]
        if not isinstance(positions, list) or not positions:
            # Empty page, or unexpected payload shape (rate-limit / interstitial HTML).
            break

        for item in positions:
            if not isinstance(item, list) or len(item) < 2:
                continue
            job_id = str(item[0] or "").strip()
            title = str(item[1] or "").strip()
            if not job_id or not title:
                continue

            item_company = str(item[7]).strip() if len(item) > 7 and item[7] else company_name
            location = _google_locations(item[9]) if len(item) > 9 else ""
            requirements = ""
            if len(item) > 4:
                requirements = _google_html_blob(item[4])
            if not requirements and len(item) > 19:
                requirements = _google_html_blob(item[19])

            jobs.append(
                normalize_job(
                    job_id=f"google:{job_id}",
                    title=title,
                    company=item_company or company_name,
                    url=f"{_GOOGLE_CAREERS_URL}{job_id}",
                    location=location,
                    requirements=requirements,
                )
            )

        if len(positions) < page_size:
            break
        if total is not None and len(jobs) >= total:
            break
        page += 1

    print(f"[google] fetched {len(jobs)} jobs")
    return jobs


_UBER_API_URL = "https://www.uber.com/api/loadSearchJobsResults"
_UBER_JOB_URL = "https://jobs.uber.com/en/jobs/{job_id}"


def _uber_format_location(loc: Any) -> str:
    """Format one Uber location dict into a display string."""
    if not isinstance(loc, dict):
        return str(loc).strip() if loc else ""
    parts: list[str] = []
    for key in ("city", "region", "countryName"):
        val = loc.get(key)
        if val and str(val).strip():
            parts.append(str(val).strip())
    if not loc.get("countryName"):
        country = loc.get("country")
        if country and str(country).strip():
            parts.append(str(country).strip())
    return ", ".join(parts)


def _uber_locations(item: dict[str, Any]) -> str:
    """Join Uber allLocations (or primary location) for filtering."""
    locs = item.get("allLocations")
    if isinstance(locs, list) and locs:
        formatted = [_uber_format_location(loc) for loc in locs]
        return " | ".join(x for x in formatted if x)
    return _uber_format_location(item.get("location"))


def _uber_total_results(payload: dict[str, Any]) -> int | None:
    """Parse Uber totalResults (plain int or protobuf-style {low, high})."""
    raw = payload.get("totalResults")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, dict) and "low" in raw:
        try:
            return int(raw["low"])
        except (TypeError, ValueError):
            return None
    return None


def fetch_uber(
    company: str | None = None,
    *,
    page_size: int = 50,
) -> list[dict[str, str]]:
    """Fetch jobs from Uber's first-party careers search API.

    List results omit descriptions; filters here are title/location only.
    """
    company_name = company or "Uber"
    headers = {
        "x-csrf-token": "x",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params = {"localeCode": "en"}
    jobs: list[dict[str, str]] = []
    page = 0
    total: int | None = None

    while True:
        try:
            resp = requests.post(
                _UBER_API_URL,
                params=params,
                headers=headers,
                json={"limit": page_size, "page": page, "params": {}},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[uber] fetch failed at page={page}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[uber] invalid JSON at page={page}: {exc}", file=sys.stderr)
            break

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            print(f"[uber] unexpected payload at page={page}", file=sys.stderr)
            break

        if total is None:
            total = _uber_total_results(data)

        results = data.get("results") or []
        if not results:
            break

        for item in results:
            if not isinstance(item, dict):
                continue
            job_id = item.get("id")
            if job_id is None or job_id == "":
                continue
            jobs.append(
                normalize_job(
                    job_id=f"uber:{job_id}",
                    title=(item.get("title") or "").strip() or "Untitled",
                    company=company_name,
                    url=_UBER_JOB_URL.format(job_id=job_id),
                    location=_uber_locations(item),
                    requirements=(item.get("description") or "").strip(),
                )
            )

        page += 1
        if total is not None and len(jobs) >= total:
            break
        if len(results) < page_size:
            break
        if page > 200:
            print("[uber] abort: pagination exceeded 200 pages", file=sys.stderr)
            break

    print(f"[uber] fetched {len(jobs)} jobs")
    return jobs


_META_SITEMAP_URL = "https://www.metacareers.com/jobsearch/sitemap.xml"
_META_JOB_URL = "https://www.metacareers.com/jobs/{job_id}"
_META_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _meta_locations(job_location: Any) -> str:
    """Join Meta JSON-LD jobLocation place names."""
    if isinstance(job_location, dict):
        job_location = [job_location]
    if not isinstance(job_location, list):
        return ""
    names: list[str] = []
    for loc in job_location:
        if isinstance(loc, dict):
            name = (loc.get("name") or "").strip()
            if name:
                names.append(name)
        elif isinstance(loc, str) and loc.strip():
            names.append(loc.strip())
    return ", ".join(names)


def _meta_parse_detail(html: str, job_id: str, company_name: str) -> dict[str, str] | None:
    """Parse a Meta job detail page (JSON-LD JobPosting) into a normalized job."""
    match = _META_JSONLD_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = next((x for x in data if isinstance(x, dict) and x.get("@type") == "JobPosting"), None)
    if not isinstance(data, dict) or data.get("@type") != "JobPosting":
        return None
    title = (data.get("title") or data.get("name") or "").strip()
    if not title:
        return None
    return normalize_job(
        job_id=f"meta:{job_id}",
        title=title,
        company=company_name,
        url=_META_JOB_URL.format(job_id=job_id),
        location=_meta_locations(data.get("jobLocation")),
        requirements=(data.get("description") or data.get("qualifications") or "").strip(),
    )


def _meta_fetch_detail(job_id: str, company_name: str) -> dict[str, str] | None:
    """Fetch one Meta job detail page with Chrome TLS impersonation."""
    assert curl_requests is not None
    url = _META_JOB_URL.format(job_id=job_id)
    try:
        resp = curl_requests.get(url, impersonate="chrome", timeout=45)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[meta] detail failed for {job_id}: {exc}", file=sys.stderr)
        return None
    return _meta_parse_detail(resp.text, job_id, company_name)


def fetch_meta(
    company: str | None = None,
    *,
    workers: int = 8,
) -> list[dict[str, str]]:
    """Fetch jobs from Meta Careers via sitemap + JSON-LD detail pages.

    Meta blocks plain ``requests`` (TLS fingerprint); uses curl_cffi Chrome
    impersonation. Listings are not in a public JSON API — the jobsearch
    sitemap enumerates openings, and each detail page embeds JobPosting JSON-LD.
    """
    if curl_requests is None:
        print(
            "[meta] curl_cffi is required (pip install curl-cffi)",
            file=sys.stderr,
        )
        return []

    company_name = company or "Meta"
    try:
        resp = curl_requests.get(_META_SITEMAP_URL, impersonate="chrome", timeout=60)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[meta] sitemap fetch failed: {exc}", file=sys.stderr)
        return []

    job_ids = list(dict.fromkeys(re.findall(r"job_details/(\d+)", resp.text)))
    if not job_ids:
        print("[meta] sitemap contained no job ids", file=sys.stderr)
        return []

    jobs: list[dict[str, str]] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_meta_fetch_detail, job_id, company_name): job_id for job_id in job_ids
        }
        for fut in as_completed(futures):
            job = fut.result()
            if job is None:
                failures += 1
                continue
            jobs.append(job)

    print(f"[meta] fetched {len(jobs)} jobs ({failures} detail failures)")
    return jobs


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_workday(
    myworkday_host: str,
    site: str,
    company: str | None = None,
    *,
    page_size: int = 20,
) -> list[dict[str, str]]:
    """Fetch jobs from a Workday CXS public careers board.

    ``myworkday_host`` is like ``nvidia.wd5.myworkdayjobs.com``;
    ``site`` is the board path segment (e.g. ``NVIDIAExternalCareerSite``).
    """
    host = myworkday_host.replace("https://", "").replace("http://", "").strip("/")
    # CXS path uses tenant = first DNS label (nvidia, snapchat, …).
    tenant = host.split(".", 1)[0]
    api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    company_name = company or site
    jobs: list[dict[str, str]] = []
    offset = 0
    total: int | None = None

    while True:
        try:
            resp = requests.post(
                api_url,
                json={
                    "appliedFacets": {},
                    "limit": page_size,
                    "offset": offset,
                    "searchText": "",
                },
                headers={
                    **_BROWSER_HEADERS,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[workday:{site}] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[workday:{site}] invalid JSON at offset={offset}: {exc}", file=sys.stderr)
            break

        if total is None:
            raw_total = payload.get("total")
            try:
                total = int(raw_total) if raw_total is not None else None
            except (TypeError, ValueError):
                total = None

        postings = payload.get("jobPostings") or []
        if not postings:
            break

        for item in postings:
            if not isinstance(item, dict):
                continue
            external_path = (item.get("externalPath") or "").strip()
            bullets = item.get("bulletFields") or []
            job_id = ""
            if isinstance(bullets, list) and bullets:
                job_id = str(bullets[0] or "").strip()
            if not job_id and external_path:
                job_id = external_path.rsplit("_", 1)[-1] or external_path
            if not job_id:
                continue
            url = (
                f"https://{host}/{site}{external_path}"
                if external_path
                else f"https://{host}/{site}"
            )
            jobs.append(
                normalize_job(
                    job_id=f"workday:{site}:{job_id}",
                    title=(item.get("title") or "").strip() or "Untitled",
                    company=company_name,
                    url=url,
                    location=(item.get("locationsText") or "").strip(),
                )
            )

        offset += len(postings)
        if total is not None and offset >= total:
            break
        if len(postings) < page_size:
            break
        if offset > 10_000:
            print(f"[workday:{site}] abort: pagination exceeded 10000", file=sys.stderr)
            break

    print(f"[workday:{site}] fetched {len(jobs)} jobs")
    return jobs


def fetch_amazon(
    company: str | None = None,
    *,
    page_size: int = 100,
) -> list[dict[str, str]]:
    """Fetch jobs from Amazon.jobs public search JSON API."""
    company_name = company or "Amazon"
    api_url = "https://www.amazon.jobs/en/search.json"
    jobs: list[dict[str, str]] = []
    offset = 0
    total: int | None = None

    while True:
        try:
            resp = requests.get(
                api_url,
                params={"offset": offset, "result_limit": page_size, "sort": "relevant"},
                headers={**_BROWSER_HEADERS, "Accept": "application/json"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[amazon] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[amazon] invalid JSON at offset={offset}: {exc}", file=sys.stderr)
            break

        if total is None:
            raw_hits = payload.get("hits")
            try:
                total = int(raw_hits) if raw_hits is not None else None
            except (TypeError, ValueError):
                total = None

        results = payload.get("jobs") or []
        if not results:
            break

        for item in results:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id_icims") or item.get("id") or "").strip()
            path = (item.get("job_path") or "").strip()
            if not job_id and path:
                job_id = path
            if not job_id:
                continue
            url = f"https://www.amazon.jobs{path}" if path else ""
            location = (
                (item.get("normalized_location") or item.get("location") or "").strip()
            )
            requirements = "\n\n".join(
                part
                for part in (
                    (item.get("basic_qualifications") or "").strip(),
                    (item.get("preferred_qualifications") or "").strip(),
                    (item.get("description") or "").strip(),
                )
                if part
            )
            jobs.append(
                normalize_job(
                    job_id=f"amazon:{job_id}",
                    title=(item.get("title") or "").strip() or "Untitled",
                    company=(item.get("company_name") or "").strip() or company_name,
                    url=url,
                    location=location,
                    requirements=requirements,
                )
            )

        offset += len(results)
        if total is not None and offset >= total:
            break
        if len(results) < page_size:
            break
        if offset > 15_000:
            print("[amazon] abort: pagination exceeded 15000", file=sys.stderr)
            break

    print(f"[amazon] fetched {len(jobs)} jobs")
    return jobs


_APPLE_SEARCH_URL = "https://jobs.apple.com/en-us/search"
_APPLE_HYDRATION_RE = re.compile(
    r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(\"(.+?)\"\);\s*</script>",
    re.DOTALL,
)


def _apple_parse_hydration(html: str) -> tuple[list[dict[str, Any]], int | None] | None:
    """Parse Apple careers SSR hydration JSON into (results, totalRecords)."""
    match = _APPLE_HYDRATION_RE.search(html)
    if not match:
        return None
    try:
        # JSON.parse("...") payload is a JS string literal; decode then parse.
        raw = json.loads(f'"{match.group(1)}"')
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    search = (data.get("loaderData") or {}).get("search") or {}
    results = search.get("searchResults") or []
    if not isinstance(results, list):
        return None
    total: int | None = None
    raw_total = search.get("totalRecords")
    try:
        total = int(raw_total) if raw_total is not None else None
    except (TypeError, ValueError):
        total = None
    return results, total


def _apple_locations(locations: Any) -> str:
    if not isinstance(locations, list):
        return ""
    names: list[str] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        name = (loc.get("name") or "").strip()
        if not name:
            parts = [
                (loc.get("city") or "").strip(),
                (loc.get("stateProvince") or "").strip(),
                (loc.get("countryName") or "").strip(),
            ]
            name = ", ".join(p for p in parts if p)
        if name:
            names.append(name)
    return ", ".join(names)


def fetch_apple(
    company: str | None = None,
    *,
    page_size: int = 20,
) -> list[dict[str, str]]:
    """Fetch jobs from Apple Careers SSR hydration embedded in search HTML.

    Apple's ``/api/role/search`` endpoint is unreliable from automation; the
    public search pages embed the full result list in ``__staticRouterHydrationData``.
    """
    company_name = company or "Apple"
    jobs: list[dict[str, str]] = []
    page = 1
    total: int | None = None
    max_pages = 500

    while page <= max_pages:
        try:
            resp = requests.get(
                _APPLE_SEARCH_URL,
                params={"search": "", "sort": "newest", "page": page},
                headers={**_BROWSER_HEADERS, "Accept": "text/html"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[apple] fetch failed at page={page}: {exc}", file=sys.stderr)
            break

        parsed = _apple_parse_hydration(resp.text)
        if parsed is None:
            print(f"[apple] no hydration payload at page={page}", file=sys.stderr)
            break
        results, page_total = parsed
        if total is None:
            total = page_total
        if not results:
            break

        for item in results:
            if not isinstance(item, dict):
                continue
            position_id = str(item.get("positionId") or "").strip()
            job_key = str(item.get("id") or position_id).strip()
            title = (item.get("postingTitle") or item.get("transformedPostingTitle") or "").strip()
            if not job_key or not title:
                continue
            detail_id = position_id or job_key.removeprefix("PIPE-")
            jobs.append(
                normalize_job(
                    job_id=f"apple:{job_key}",
                    title=title,
                    company=company_name,
                    url=f"https://jobs.apple.com/en-us/details/{detail_id}",
                    location=_apple_locations(item.get("locations")),
                    requirements=(item.get("jobSummary") or "").strip(),
                )
            )

        if len(results) < page_size:
            break
        if total is not None and len(jobs) >= total:
            break
        page += 1

    print(f"[apple] fetched {len(jobs)} jobs")
    return jobs


def fetch_microsoft(
    company: str | None = None,
    *,
    domain: str = "microsoft.com",
    host: str = "apply.careers.microsoft.com",
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch jobs from Microsoft Careers Eightfold PCSX search API."""
    company_name = company or "Microsoft"
    api_url = f"https://{host}/api/pcsx/search"
    jobs: list[dict[str, str]] = []
    start = 0
    total: int | None = None

    while True:
        try:
            resp = requests.get(
                api_url,
                params={"domain": domain, "start": start, "num": page_size},
                headers={
                    **_BROWSER_HEADERS,
                    "Accept": "application/json",
                    "Referer": f"https://{host}/careers/jobs",
                },
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[microsoft] fetch failed at start={start}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[microsoft] invalid JSON at start={start}: {exc}", file=sys.stderr)
            break

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            print(f"[microsoft] unexpected payload at start={start}", file=sys.stderr)
            break

        if total is None:
            raw_count = data.get("count")
            try:
                total = int(raw_count) if raw_count is not None else None
            except (TypeError, ValueError):
                total = None

        positions = data.get("positions") or []
        if not positions:
            break

        for item in positions:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id") or item.get("displayJobId") or "").strip()
            if not job_id:
                continue
            locs = item.get("locations") or item.get("standardizedLocations") or []
            if isinstance(locs, list):
                location = ", ".join(str(x) for x in locs if x)
            else:
                location = str(locs or "").strip()
            path = (item.get("positionUrl") or "").strip()
            url = f"https://{host}{path}" if path.startswith("/") else path
            if not url:
                url = f"https://{host}/careers/job/{job_id}"
            jobs.append(
                normalize_job(
                    job_id=f"microsoft:{job_id}",
                    title=(item.get("name") or "").strip() or "Untitled",
                    company=company_name,
                    url=url,
                    location=location,
                )
            )

        start += len(positions)
        if total is not None and start >= total:
            break
        # PCSX often ignores larger page sizes and returns a fixed ~10.
        if len(positions) < 1:
            break
        if start > 20_000:
            print("[microsoft] abort: pagination exceeded 20000", file=sys.stderr)
            break

    print(f"[microsoft] fetched {len(jobs)} jobs")
    return jobs


def fetch_bytedance(
    company: str | None = None,
    *,
    host: str = "jobs.bytedance.com",
    portal_type: int = 2,
    keyword: str = "",
    page_size: int = 50,
) -> list[dict[str, str]]:
    """Fetch jobs from ByteDance / TikTok careers search API.

    Requires ``curl_cffi`` (Chrome TLS). ``portal_type`` is accepted by the API
    but often ignored; use ``keyword`` (e.g. ``\"TikTok\"``) to narrow brands.
    """
    if curl_requests is None:
        print(
            "[bytedance] curl_cffi is required (pip install curl-cffi)",
            file=sys.stderr,
        )
        return []

    company_name = company or ("TikTok" if "tiktok" in keyword.lower() else "ByteDance")
    host = host.replace("https://", "").replace("http://", "").strip("/")
    origin = f"https://{host}"
    list_path = "/en/position" if "bytedance" in host else "/position"
    jobs: list[dict[str, str]] = []
    offset = 0
    total: int | None = None
    label = f"{host}:kw={keyword or '*'}"

    session = curl_requests.Session(impersonate="chrome")
    try:
        session.get(f"{origin}{list_path}", timeout=45)
    except Exception as exc:
        print(f"[bytedance:{label}] session warmup failed: {exc}", file=sys.stderr)

    while True:
        body = {
            "job_category_id_list": [],
            "keyword": keyword or "",
            "limit": page_size,
            "location_code_list": [],
            "offset": offset,
            "portal_entrance": 1,
            "portal_type": portal_type,
            "recruitment_id_list": [],
            "subject_id_list": [],
        }
        try:
            resp = session.post(
                f"{origin}/api/v1/search/job/posts",
                json=body,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "Referer": f"{origin}{list_path}",
                },
                timeout=45,
            )
            resp.raise_for_status()
        except Exception as exc:
            print(f"[bytedance:{label}] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[bytedance:{label}] invalid JSON at offset={offset}: {exc}", file=sys.stderr)
            break

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            print(f"[bytedance:{label}] unexpected payload at offset={offset}", file=sys.stderr)
            break

        if total is None:
            raw_count = data.get("count")
            try:
                total = int(raw_count) if raw_count is not None else None
            except (TypeError, ValueError):
                total = None

        posts = data.get("job_post_list") or []
        if not posts:
            break

        for item in posts:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id") or item.get("code") or "").strip()
            if not job_id:
                continue
            city = item.get("city_info") or {}
            location = ""
            if isinstance(city, dict):
                location = (city.get("en_name") or city.get("name") or "").strip()
            requirements = "\n\n".join(
                part
                for part in (
                    (item.get("description") or "").strip(),
                    (item.get("requirement") or "").strip(),
                )
                if part
            )
            jobs.append(
                normalize_job(
                    job_id=f"bytedance:{job_id}",
                    title=(item.get("title") or "").strip() or "Untitled",
                    company=company_name,
                    url=f"{origin}/en/position/{job_id}"
                    if "bytedance" in host
                    else f"{origin}/position/{job_id}",
                    location=location,
                    requirements=requirements,
                )
            )

        offset += len(posts)
        if total is not None and offset >= total:
            break
        if len(posts) < page_size:
            break
        if offset > 15_000:
            print(f"[bytedance:{label}] abort: pagination exceeded 15000", file=sys.stderr)
            break

    print(f"[bytedance:{label}] fetched {len(jobs)} jobs")
    return jobs


def fetch_icims_api(
    api_url: str,
    company: str | None = None,
    *,
    page_size: int = 100,
) -> list[dict[str, str]]:
    """Fetch jobs from an iCIMS-backed public JSON careers API (e.g. SIG)."""
    company_name = company or "iCIMS"
    jobs: list[dict[str, str]] = []
    page = 1
    total: int | None = None
    seen_slugs: set[str] = set()

    while True:
        try:
            resp = requests.get(
                api_url,
                params={"page": page, "limit": page_size},
                headers={**_BROWSER_HEADERS, "Accept": "application/json"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[icims_api] fetch failed at page={page}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[icims_api] invalid JSON at page={page}: {exc}", file=sys.stderr)
            break

        if total is None:
            raw = payload.get("count") or payload.get("totalCount")
            try:
                total = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                total = None

        results = payload.get("jobs") or []
        if not results:
            break

        new_on_page = 0
        for wrap in results:
            item = wrap.get("data") if isinstance(wrap, dict) else None
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or item.get("req_id") or "").strip()
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            new_on_page += 1
            location = (
                (item.get("full_location") or item.get("location_name") or item.get("city") or "")
                .strip()
            )
            url = (item.get("apply_url") or "").strip()
            if not url and item.get("req_id"):
                url = f"https://careers-sig.icims.com/jobs/{item['req_id']}"
            jobs.append(
                normalize_job(
                    job_id=f"icims:{slug}",
                    title=(item.get("title") or "").strip() or "Untitled",
                    company=company_name,
                    url=url,
                    location=location,
                    requirements=(item.get("description") or item.get("qualifications") or "").strip(),
                )
            )

        if new_on_page == 0:
            break
        if total is not None and len(jobs) >= total:
            break
        page += 1
        if page > 200:
            print("[icims_api] abort: pagination exceeded 200 pages", file=sys.stderr)
            break

    print(f"[icims_api:{company_name}] fetched {len(jobs)} jobs")
    return jobs


def fetch_bloomberg(
    company: str | None = None,
    *,
    search_url: str = "https://bloomberg.avature.net/careers/SearchJobs/",
    page_size: int = 12,
) -> list[dict[str, str]]:
    """Fetch jobs from Bloomberg Avature SearchJobs HTML (jobOffset pagination)."""
    company_name = company or "Bloomberg"
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    offset = 0
    max_offset = 5_000

    while offset <= max_offset:
        try:
            resp = requests.get(
                search_url,
                params={
                    "listFilterMode": 1,
                    "jobRecordsPerPage": page_size,
                    "jobOffset": offset,
                },
                headers={**_BROWSER_HEADERS, "Accept": "text/html"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[bloomberg] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        # Title+url pairs from JobDetail anchors (skip bare "Apply" CTAs).
        pairs = re.findall(
            r'href="(https://bloomberg\.avature\.net/careers/JobDetail/[^"]+/(\d+))"[^>]*>\s*([^<]+)',
            resp.text,
        )
        new_on_page = 0
        for url, job_id, title in pairs:
            clean_title = re.sub(r"\s+", " ", title).strip()
            if not clean_title or clean_title.lower() == "apply":
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            new_on_page += 1
            jobs.append(
                normalize_job(
                    job_id=f"bloomberg:{job_id}",
                    title=clean_title,
                    company=company_name,
                    url=url,
                )
            )

        if new_on_page == 0:
            break
        offset += page_size

    print(f"[bloomberg] fetched {len(jobs)} jobs")
    return jobs


def fetch_linkedin(
    company: str | None = None,
    *,
    company_ids: str = "1337",
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch LinkedIn company jobs via the public jobs-guest HTML API."""
    company_name = company or "LinkedIn"
    api_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    start = 0
    empty_streak = 0

    while start < 2_000:
        try:
            resp = requests.get(
                api_url,
                params={"f_C": company_ids, "start": start, "geoId": "92000000"},
                headers={**_BROWSER_HEADERS, "Accept": "text/html"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[linkedin] fetch failed at start={start}: {exc}", file=sys.stderr)
            break

        html = resp.text
        ids = re.findall(r'data-entity-urn="urn:li:jobPosting:(\d+)"', html)
        titles = [
            re.sub(r"\s+", " ", t).strip()
            for t in re.findall(
                r'class="[^"]*base-search-card__title[^"]*"[^>]*>\s*([^<]+)', html
            )
        ]
        locs = [
            re.sub(r"\s+", " ", t).strip()
            for t in re.findall(
                r'class="[^"]*job-search-card__location[^"]*"[^>]*>\s*([^<]+)', html
            )
        ]
        hrefs = re.findall(
            r'href="(https://(?:www\.)?linkedin\.com/jobs/view/[^"?]+)', html
        )

        if not ids:
            break

        new_on_page = 0
        for i, job_id in enumerate(ids):
            if job_id in seen:
                continue
            seen.add(job_id)
            new_on_page += 1
            title = titles[i] if i < len(titles) else "Untitled"
            location = locs[i] if i < len(locs) else ""
            url = hrefs[i] if i < len(hrefs) else f"https://www.linkedin.com/jobs/view/{job_id}"
            jobs.append(
                normalize_job(
                    job_id=f"linkedin:{job_id}",
                    title=title or "Untitled",
                    company=company_name,
                    url=url,
                    location=location,
                )
            )

        if new_on_page == 0:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0

        start += max(len(ids), page_size)

    print(f"[linkedin] fetched {len(jobs)} jobs")
    return jobs


_CITADEL_CARD_RE = re.compile(
    r'<a\s+class="careers-listing-card[^"]*"\s+href="([^"]+)"[^>]*>'
    r'.*?aria-label="([^"]+)"[^>]*>'
    r'.*?<div class="careers-listing-card__location">\s*([^<]*?)\s*</div>',
    re.DOTALL | re.IGNORECASE,
)


def fetch_citadel_securities(
    company: str | None = None,
    *,
    url: str = "https://www.citadelsecurities.com/careers/open-opportunities/",
) -> list[dict[str, str]]:
    """Fetch Citadel Securities open roles from the careers HTML listing.

    Cloudflare often blocks plain ``requests``; uses curl_cffi when available.
    Roles are evergreen detail pages (not per-requisition ATS IDs).
    """
    company_name = company or "Citadel Securities"
    html = ""
    if curl_requests is not None:
        try:
            resp = curl_requests.get(url, impersonate="chrome", timeout=60)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            print(f"[citadel_securities] curl_cffi fetch failed: {exc}", file=sys.stderr)
    if not html:
        try:
            resp = requests.get(url, headers={**_BROWSER_HEADERS, "Accept": "text/html"}, timeout=45)
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException as exc:
            print(f"[citadel_securities] fetch failed: {exc}", file=sys.stderr)
            return []

    if "Just a moment" in html and "cf-" in html.lower():
        print(
            "[citadel_securities] blocked by Cloudflare; install/use curl-cffi",
            file=sys.stderr,
        )
        return []

    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title, location in _CITADEL_CARD_RE.findall(html):
        clean_title = (
            re.sub(r"\s+", " ", title)
            .replace("&#8211;", "–")
            .replace("&amp;", "&")
            .strip()
        )
        if not clean_title:
            continue
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        job_id = slug or clean_title
        if job_id in seen:
            continue
        seen.add(job_id)
        jobs.append(
            normalize_job(
                job_id=f"citadel_securities:{job_id}",
                title=clean_title,
                company=company_name,
                url=href if href.startswith("http") else f"https://www.citadelsecurities.com{href}",
                location=re.sub(r"\s+", " ", location).strip(),
            )
        )

    print(f"[citadel_securities] fetched {len(jobs)} jobs")
    return jobs


def fetch_placeholder(config: dict[str, Any]) -> list[dict[str, str]]:
    """Sample jobs so the pipeline can be smoke-tested when live sources return nothing."""
    titles = config.get("titles") or ["Research Scientist"]
    sample_title = titles[0]
    return [
        normalize_job(
            job_id="placeholder:sample-us-match",
            title=sample_title,
            company="Example Labs",
            url="https://example.com/jobs/sample-us-match",
            location="Remote, United States",
            requirements=(
                "Requirements:\n"
                "- Ph.D. in Electrical and Computer Engineering or related field\n"
                "- Experience with machine learning and mathematical optimization\n"
                "- Strong Python skills (Pandas, NumPy, Scikit-Learn)\n"
                "- Research experience in sequential decision-making or bandits\n"
            ),
        ),
        normalize_job(
            job_id="placeholder:sample-eu-reject",
            title="ML Engineer",
            company="Example EU GmbH",
            url="https://example.com/jobs/sample-eu-reject",
            location="Berlin, Germany",
            requirements=(
                "Requirements:\n"
                "- 5+ years production ML experience\n"
                "- Ph.D. preferred\n"
                "- Must be based in Germany\n"
            ),
        ),
        normalize_job(
            job_id="placeholder:sample-missing-skills",
            title="Quantitative Researcher",
            company="Example Quant",
            url="https://example.com/jobs/sample-missing-skills",
            location="New York, NY, United States",
            requirements=(
                "Requirements:\n"
                "- 3+ years of C++ low-latency trading systems experience\n"
                "- Deep expertise in FPGA hardware acceleration\n"
                "- Prior market-making desk experience required\n"
            ),
        ),
    ]


def _normalize_title_text(text: str) -> str:
    """Lowercase and expand common ML/AI abbreviations for substring matching."""
    t = (text or "").lower()
    replacements = (
        ("machine learning", " ml "),
        ("artificial intelligence", " ai "),
        ("ops research", " operations research "),
        ("post-doc", " postdoc "),
        ("post doctoral", " postdoc "),
        ("phd", " phd "),
        ("ph.d.", " phd "),
        ("ph.d", " phd "),
    )
    for old, new in replacements:
        t = t.replace(old, new)
    return " ".join(t.split())


def title_matches(job_title: str, titles: list[str]) -> bool:
    """True if job title contains any configured title phrase (fuzzy on ML/AI)."""
    if not titles:
        return True
    normalized_job = _normalize_title_text(job_title)
    for want in titles:
        needle = _normalize_title_text(want)
        if needle and needle in normalized_job:
            return True
    return False


def title_excluded(job_title: str, exclusions: list[str]) -> bool:
    """True if job title contains any seniority / experience-level exclusion phrase.

    Uses word-boundary matching so short tokens like 'IV', 'Sr', or 'VP' do not
    false-positive inside unrelated words (e.g. 'private', 'SRE').
    """
    if not exclusions:
        return False
    normalized_job = _normalize_title_text(job_title)
    for phrase in exclusions:
        needle = _normalize_title_text(phrase)
        if not needle:
            continue
        # Escape so literals like 'Sr.' match safely; require token boundaries.
        pattern = r"(?:^|[^a-z0-9])" + re.escape(needle) + r"(?:[^a-z0-9]|$)"
        if re.search(pattern, normalized_job):
            return True
    return False


# Explicit US markers and common US city/state tokens for multi-location strings.
_US_POSITIVE_PHRASES = (
    "united states",
    "usa",
    "u.s.a",
    "u.s.",
    "remote - us",
    "remote-us",
    "remote, us",
    "remote, usa",
    "remote us",
    "nationwide",
    "united states of america",
)

# Two-letter US state / DC codes. Matched as whole tokens only.
_US_STATE_CODES = frozenset(
    {
        "al", "ak", "az", "ar", "ca", "co", "ct", "dc", "de", "fl", "ga", "hi",
        "ia", "id", "il", "in", "ks", "ky", "la", "ma", "md", "me", "mi", "mn",
        "mo", "ms", "mt", "nc", "nd", "ne", "nh", "nj", "nm", "nv", "ny", "oh",
        "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "va", "vt", "wa",
        "wi", "wv", "wy",
    }
)

_US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
)

_US_CITY_PHRASES = (
    "san francisco", "new york city", "new york", "nyc", "seattle", "chicago",
    "boston", "austin", "seattle", "denver", "dallas", "atlanta", "miami",
    "los angeles", "san diego", "san jose", "palo alto", "mountain view",
    "menlo park", "redwood city", "foster city", "san mateo", "sunnyvale",
    "cupertino", "santa clara", "kirkland", "bellevue", "redmond", "seattle",
    "washington dc", "washington d.c", "washington, d.c", "st. louis",
    "st louis", "pittsburgh", "philadelphia", "phoenix", "portland", "honolulu",
    "fayetteville", "hayward", "stamford", "detroit",
)


def _token_boundary_search(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    pattern = r"(?:^|[^a-z0-9])" + re.escape(needle) + r"(?:[^a-z0-9]|$)"
    return re.search(pattern, haystack) is not None


def _location_has_us_signal(location: str) -> bool:
    """True if location text clearly includes a US-eligible option."""
    text = " ".join((location or "").lower().replace("–", "-").replace("—", "-").split())
    if not text:
        return False

    # Bare "US" / "U.S." as a token (avoid matching inside other words).
    if _token_boundary_search(text, "us") or _token_boundary_search(text, "u.s"):
        return True

    for phrase in _US_POSITIVE_PHRASES:
        if phrase in text:
            return True

    for phrase in _US_STATE_NAMES:
        if _token_boundary_search(text, phrase):
            return True

    for phrase in _US_CITY_PHRASES:
        if phrase in text:
            return True

    # State codes: prefer ", CA" / " CA," / trailing " CA" style to reduce false hits.
    for code in _US_STATE_CODES:
        if re.search(rf"(?:^|[^a-z0-9]){code}(?:[^a-z0-9]|$)", text):
            # Require comma/space context typical of "City, CA" listings when short.
            if re.search(rf",\s*{code}(?:[^a-z0-9]|$)", text) or re.search(
                rf"(?:^|[^a-z0-9]){code}\s*,", text
            ):
                return True

    return False


def location_is_us_eligible(location: str, non_us_locations: list[str]) -> bool:
    """Keep US / ambiguous locations; drop clearly non-US-only postings.

    Multi-location strings that include any US option (e.g. "London | NYC") are kept.
    Empty / bare "Remote" is kept (ambiguous) so later filters can decide.
    """
    text = " ".join((location or "").lower().replace("–", "-").replace("—", "-").split())
    if not text:
        return True

    if _location_has_us_signal(text):
        return True

    for phrase in non_us_locations:
        needle = " ".join(phrase.lower().split())
        if needle and _token_boundary_search(text, needle):
            return False

    return True


def fetch_jobs(config: dict[str, Any]) -> list[dict[str, str]]:
    """Orchestrate all configured sources; fall back to placeholders if empty."""
    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    titles = [t for t in (config.get("titles") or []) if isinstance(t, str) and t.strip()]
    exclusions = [
        t for t in (config.get("title_exclusions") or []) if isinstance(t, str) and t.strip()
    ]
    non_us_locations = [
        t for t in (config.get("non_us_locations") or []) if isinstance(t, str) and t.strip()
    ]

    for site in config.get("websites") or []:
        site_type = (site.get("type") or "").lower()
        company = site.get("name") or ""

        try:
            if site_type == "greenhouse":
                token = site.get("board_token")
                if not token:
                    continue
                fetched = fetch_greenhouse(token, company=company or token)
            elif site_type == "ashby":
                board = site.get("board_name") or site.get("board_token")
                if not board:
                    continue
                fetched = fetch_ashby(board, company=company or board)
            elif site_type == "lever":
                lever_site = site.get("site") or site.get("board_token")
                if not lever_site:
                    continue
                fetched = fetch_lever(lever_site, company=company or lever_site)
            elif site_type == "eightfold":
                domain = site.get("domain")
                host = site.get("host")
                if not host and site.get("url"):
                    host = urlparse(site["url"]).netloc
                if not domain or not host:
                    continue
                fetched = fetch_eightfold(domain, host, company=company or domain)
            elif site_type == "google":
                # Optional ATS company filter (e.g. "Google"); omit to include DeepMind etc.
                company_filter = site.get("company")
                if company_filter is None:
                    company_filter = "Google"
                fetched = fetch_google(
                    company_filter=company_filter or None,
                    display_name=company or "Google",
                )
            elif site_type == "uber":
                fetched = fetch_uber(company=company or "Uber")
            elif site_type == "meta":
                fetched = fetch_meta(company=company or "Meta")
            elif site_type == "workday":
                host = site.get("myworkday_host") or site.get("host")
                wd_site = site.get("site") or site.get("board")
                if not host and site.get("url"):
                    host = urlparse(site["url"]).netloc
                if not host or not wd_site:
                    continue
                fetched = fetch_workday(host, wd_site, company=company or wd_site)
            elif site_type == "amazon":
                fetched = fetch_amazon(company=company or "Amazon")
            elif site_type == "apple":
                fetched = fetch_apple(company=company or "Apple")
            elif site_type == "microsoft":
                fetched = fetch_microsoft(
                    company=company or "Microsoft",
                    domain=site.get("domain") or "microsoft.com",
                    host=site.get("host") or "apply.careers.microsoft.com",
                )
            elif site_type == "bytedance":
                fetched = fetch_bytedance(
                    company=company or "ByteDance",
                    host=site.get("host") or "jobs.bytedance.com",
                    portal_type=int(site.get("portal_type") or 2),
                    keyword=site.get("keyword") or "",
                )
            elif site_type == "icims_api":
                api_url = site.get("api_url")
                if not api_url:
                    continue
                fetched = fetch_icims_api(api_url, company=company or "iCIMS")
            elif site_type == "bloomberg":
                fetched = fetch_bloomberg(
                    company=company or "Bloomberg",
                    search_url=site.get("search_url")
                    or site.get("url")
                    or "https://bloomberg.avature.net/careers/SearchJobs/",
                )
            elif site_type == "linkedin":
                company_ids = site.get("company_ids") or "1337"
                if isinstance(company_ids, list):
                    company_ids = ",".join(str(x) for x in company_ids)
                fetched = fetch_linkedin(
                    company=company or "LinkedIn",
                    company_ids=str(company_ids),
                )
            elif site_type == "citadel_securities":
                fetched = fetch_citadel_securities(
                    company=company or "Citadel Securities",
                    url=site.get("url")
                    or "https://www.citadelsecurities.com/careers/open-opportunities/",
                )
            elif site_type in {"placeholder", "documentation"}:
                # Documentation / non-API targets are skipped at fetch time.
                continue
            else:
                print(f"[fetch] unknown site type {site_type!r} for {company or site}", file=sys.stderr)
                continue
        except Exception as exc:
            print(f"[fetch] {company or site_type} failed: {exc}", file=sys.stderr)
            continue

        for job in fetched:
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            jobs.append(job)

    if titles:
        before = len(jobs)
        jobs = [job for job in jobs if title_matches(job.get("title", ""), titles)]
        print(f"[filter] title match kept {len(jobs)}/{before} jobs")

    if exclusions:
        before = len(jobs)
        jobs = [job for job in jobs if not title_excluded(job.get("title", ""), exclusions)]
        print(f"[filter] title exclusion kept {len(jobs)}/{before} jobs")

    before = len(jobs)
    jobs = [
        job
        for job in jobs
        if location_is_us_eligible(job.get("location", ""), non_us_locations)
    ]
    print(f"[filter] US location kept {len(jobs)}/{before} jobs")

    if not jobs:
        print("[fetch] no live jobs returned; using placeholder samples")
        for job in fetch_placeholder(config):
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                jobs.append(job)

    return jobs


def clear_candidates(path: Path = CANDIDATES_PATH) -> None:
    """Wipe candidates.json at run start so stale results are never mixed with a new scrape."""
    if path.exists():
        path.unlink()
        print(f"Cleared {path.name}")


def write_candidates(jobs: list[dict[str, str]], path: Path = CANDIDATES_PATH) -> None:
    """Write filtered candidates for inspection (title/company/location/url only)."""
    slim = [
        {
            "id": job.get("id", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
        }
        for job in jobs
    ]
    with path.open("w", encoding="utf-8") as f:
        json.dump(slim, f, indent=2, ensure_ascii=False)
        f.write("\n")


def write_github_summary(jobs: list[dict[str, str]], *, unseen_count: int) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## Deterministic filter candidates",
        "",
        f"_Survived title filters: **{len(jobs)}** "
        f"({unseen_count} not yet in `seen_jobs.json`). No LLM scoring._",
        "",
    ]
    if not jobs:
        lines.append("_No candidates after deterministic filters._")
    else:
        lines.extend(
            [
                "| Title | Company | Location | URL |",
                "| --- | --- | --- | --- |",
            ]
        )
        for job in jobs:
            title = (job.get("title") or "").replace("|", "\\|")
            company = (job.get("company") or "").replace("|", "\\|")
            location = (job.get("location") or "").replace("|", "\\|")
            url = job.get("url") or ""
            link = f"[link]({url})" if url else ""
            lines.append(f"| {title} | {company} | {location} | {link} |")

    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    clear_candidates()
    config = load_config()
    seen = load_seen()
    candidates = fetch_jobs(config)
    unseen = [job for job in candidates if job["id"] not in seen]

    print(f"Fetched+filtered {len(candidates)} candidates; {len(unseen)} not in seen_jobs.json")
    print("(deterministic-only: no LLM, seen_jobs.json not updated)\n")

    for job in candidates:
        already = "seen" if job["id"] in seen else "new "
        print(
            f"[{already}] {job.get('company')} — {job.get('title')} "
            f"({job.get('location') or 'n/a'})"
        )
        if job.get("url"):
            print(f"         {job['url']}")

    write_candidates(candidates)
    write_github_summary(candidates, unseen_count=len(unseen))
    print(f"\nWrote {len(candidates)} candidates to {CANDIDATES_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
