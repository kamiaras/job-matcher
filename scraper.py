#!/usr/bin/env python3
"""Daily job matcher: fetch → deterministic filters → strict LLM match → persist seen_jobs.

Use --deterministic-only to stop after title/location filters and dump candidates.json
without calling Gemini or updating seen_jobs.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional until Meta is fetched
    curl_requests = None

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
RESUME_PATH = ROOT / "resume.txt"
SEEN_PATH = ROOT / "seen_jobs.json"
CANDIDATES_PATH = ROOT / "candidates.json"

# Free-tier Gemini Flash is typically ~5 RPM per model; pace requests accordingly.
# Two Flash-Lite models share the work so each model's quota can be used.
DEFAULT_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
DEFAULT_REQUEST_INTERVAL_SEC = 7.0
DEFAULT_MAX_RETRIES = 8
DEFAULT_MAX_JOBS_PER_RUN = 40

load_dotenv(ROOT / ".env")

SYSTEM_PROMPT = """You are a strict job-requirements matcher. Nothing more, nothing less.

Rules:
1. Compare ONLY the job's Requirements section against the candidate's resume lines.
2. Also verify the job location is in the US (including remote-US / nationwide US). Reject non-US locations.
3. Explicitly IGNORE salary, benefits, culture, perks, company description, and any "nice to have" framing that is outside Requirements.
4. Do NOT soft-match preferences or infer skills that are not clearly supported by a resume line.
5. A job is a match only if every hard requirement in the Requirements section is supported by the resume AND the location is US-eligible.
6. List any unmet hard requirements in missing_requirements. If is_match is true, missing_requirements should be empty.
7. Keep reason short (one or two sentences).
"""


class MatchResult(BaseModel):
    is_match: bool
    missing_requirements: list[str] = Field(default_factory=list)
    reason: str


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_resume(path: Path = RESUME_PATH) -> list[str]:
    lines: list[str] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


def load_seen(path: Path = SEEN_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def save_seen(seen: dict[str, Any], path: Path = SEEN_PATH) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)
        f.write("\n")


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


def fetch_greenhouse(
    board_token: str,
    company: str | None = None,
    *,
    metadata_includes: list[str] | None = None,
) -> list[dict[str, str]]:
    """Fetch jobs from Greenhouse public boards API.

    ``metadata_includes`` optionally keeps only jobs whose Greenhouse metadata
    values contain any of the given needles (e.g. W&B roles on CoreWeave's board).
    """
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
    needles = [n.lower() for n in (metadata_includes or []) if n and str(n).strip()]
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        if needles:
            meta_parts: list[str] = []
            for meta in item.get("metadata") or []:
                if isinstance(meta, dict) and meta.get("value") is not None:
                    meta_parts.append(str(meta["value"]))
            meta_blob = " ".join(meta_parts).lower()
            if not any(n in meta_blob for n in needles):
                continue

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
    r'<a\s+([^>]*class="[^"]*careers-listing-card[^"]*"[^>]*)>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_citadel_listing_cards(html: str, *, id_prefix: str, company_name: str) -> list[dict[str, str]]:
    """Parse Citadel / Citadel Securities evergreen careers-listing-card HTML."""
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _CITADEL_CARD_RE.finditer(html):
        attrs, body = match.group(1), match.group(2)
        href_m = re.search(r'href="([^"]+)"', attrs)
        if not href_m:
            continue
        href = href_m.group(1).strip()

        title = ""
        for pat in (
            r'data-position="([^"]+)"',
            r'aria-label="([^"]+)"',
            r'<h2[^>]*aria-label="([^"]+)"',
            r"<h2[^>]*>([^<]+)",
        ):
            title_m = re.search(pat, attrs + body, re.IGNORECASE)
            if title_m:
                title = title_m.group(1)
                break
        if not title:
            continue

        title = unescape(title.replace("&#8211;", "–").replace("&amp;", "&"))
        title = re.sub(r"^\s*Apply to\s+", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue

        loc_m = re.search(
            r'careers-listing-card__location[^>]*>\s*([^<]+)',
            body,
            re.IGNORECASE,
        )
        location = re.sub(r"\s+", " ", loc_m.group(1)).strip() if loc_m else ""

        slug = href.rstrip("/").rsplit("/", 1)[-1] or title
        if slug in seen:
            continue
        seen.add(slug)

        if href.startswith("/"):
            # Prefer host from an absolute sibling URL in the same page when possible.
            host_m = re.search(r'https://www\.(citadel(?:securities)?\.com)/', html)
            host = host_m.group(0).rstrip("/") if host_m else "https://www.citadel.com"
            href = f"{host}{href}"

        jobs.append(
            normalize_job(
                job_id=f"{id_prefix}:{slug}",
                title=title,
                company=company_name,
                url=href,
                location=location,
            )
        )
    return jobs


def _fetch_citadel_html(
    *,
    url: str,
    company_name: str,
    id_prefix: str,
    log_name: str,
) -> list[dict[str, str]]:
    """Fetch Citadel-family careers HTML (Cloudflare-prone; prefer curl_cffi)."""
    html = ""
    if curl_requests is not None:
        try:
            resp = curl_requests.get(url, impersonate="chrome", timeout=60)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            print(f"[{log_name}] curl_cffi fetch failed: {exc}", file=sys.stderr)
    if not html:
        try:
            resp = requests.get(
                url, headers={**_BROWSER_HEADERS, "Accept": "text/html"}, timeout=45
            )
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException as exc:
            print(f"[{log_name}] fetch failed: {exc}", file=sys.stderr)
            return []

    if "Just a moment" in html and "cf-" in html.lower():
        print(
            f"[{log_name}] blocked by Cloudflare; install/use curl-cffi",
            file=sys.stderr,
        )
        return []

    jobs = _parse_citadel_listing_cards(html, id_prefix=id_prefix, company_name=company_name)
    print(f"[{log_name}] fetched {len(jobs)} jobs")
    return jobs


def fetch_citadel_securities(
    company: str | None = None,
    *,
    url: str = "https://www.citadelsecurities.com/careers/open-opportunities/",
) -> list[dict[str, str]]:
    """Fetch Citadel Securities open roles from the careers HTML listing."""
    return _fetch_citadel_html(
        url=url,
        company_name=company or "Citadel Securities",
        id_prefix="citadel_securities",
        log_name="citadel_securities",
    )


def fetch_citadel(
    company: str | None = None,
    *,
    url: str = "https://www.citadel.com/careers/open-opportunities/",
) -> list[dict[str, str]]:
    """Fetch Citadel (hedge fund) open roles from the careers HTML listing."""
    return _fetch_citadel_html(
        url=url,
        company_name=company or "Citadel",
        id_prefix="citadel",
        log_name="citadel",
    )


def fetch_twosigma(
    company: str | None = None,
    *,
    search_url: str = "https://careers.twosigma.com/careers/OpenRoles/",
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch Two Sigma roles from the Avature OpenRoles HTML listing."""
    company_name = company or "Two Sigma"
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
            print(f"[twosigma] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        articles = re.findall(r"<article[^>]*>(.*?)</article>", resp.text, re.DOTALL | re.IGNORECASE)
        new_on_page = 0
        for article in articles:
            link_m = re.search(
                r'href="(https://careers\.twosigma\.com/careers/JobDetail/[^"]+/(\d+))"\s*>\s*([^<]+?)\s*</a>',
                article,
            )
            if not link_m:
                continue
            url, job_id, title = link_m.group(1), link_m.group(2), link_m.group(3)
            clean_title = unescape(re.sub(r"\s+", " ", title)).strip()
            if not clean_title or clean_title.lower() == "view role":
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            new_on_page += 1

            loc_m = re.search(
                r'class="paragraph_inner-span"[^>]*>\s*([^<]+)',
                article,
                re.IGNORECASE,
            )
            location = re.sub(r"\s+", " ", loc_m.group(1)).strip() if loc_m else ""
            jobs.append(
                normalize_job(
                    job_id=f"twosigma:{job_id}",
                    title=clean_title,
                    company=company_name,
                    url=url,
                    location=location,
                )
            )

        if new_on_page == 0:
            break
        offset += page_size

    print(f"[twosigma] fetched {len(jobs)} jobs")
    return jobs


def fetch_deshaw(
    company: str | None = None,
    *,
    url: str = "https://www.deshaw.com/careers",
    include_internships: bool = True,
) -> list[dict[str, str]]:
    """Fetch D. E. Shaw roles from the careers page ``__NEXT_DATA__`` payload."""
    company_name = company or "D. E. Shaw"
    try:
        resp = requests.get(url, headers={**_BROWSER_HEADERS, "Accept": "text/html"}, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[deshaw] fetch failed: {exc}", file=sys.stderr)
        return []

    next_m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    if not next_m:
        print("[deshaw] __NEXT_DATA__ not found", file=sys.stderr)
        return []

    try:
        payload = json.loads(next_m.group(1))
    except json.JSONDecodeError as exc:
        print(f"[deshaw] __NEXT_DATA__ JSON parse failed: {exc}", file=sys.stderr)
        return []

    page_props = (payload.get("props") or {}).get("pageProps") or {}
    raw_lists: list[Any] = []
    raw_lists.extend(page_props.get("regularJobs") or [])
    if include_internships:
        raw_lists.extend(page_props.get("internships") or [])

    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_lists:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "")
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)

        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        title = (item.get("displayName") or item.get("header") or "").strip()
        if isinstance(title, list):
            title = " ".join(str(x) for x in title if x).strip()
        if not title:
            continue

        offices = item.get("office") or []
        location_parts: list[str] = []
        if isinstance(offices, list):
            for office in offices:
                if isinstance(office, dict):
                    name = (office.get("name") or office.get("abbreviation") or "").strip()
                    if name:
                        location_parts.append(name)
                elif office:
                    location_parts.append(str(office))
        meta = data.get("jobMetadata") if isinstance(data.get("jobMetadata"), dict) else {}
        for loc in meta.get("jobLocations") or []:
            if isinstance(loc, dict):
                name = (loc.get("name") or loc.get("abbreviation") or "").strip()
                if name and name not in location_parts:
                    location_parts.append(name)

        job_slug = (data.get("jobUrl") or "").strip()
        job_url = (
            f"https://www.deshaw.com/careers/{job_slug}"
            if job_slug
            else f"https://www.deshaw.com/careers"
        )

        desc = data.get("jobDescription") if isinstance(data.get("jobDescription"), dict) else {}
        requirements = (
            desc.get("peopleWeAreLookingForStr")
            or desc.get("peopleWeAreLookingFor")
            or desc.get("responsibilities")
            or desc.get("websiteDescription")
            or ""
        )
        if isinstance(requirements, dict):
            requirements = requirements.get("text") or requirements.get("html") or ""
        requirements = str(requirements or "").strip()

        jobs.append(
            normalize_job(
                job_id=f"deshaw:{job_id}",
                title=title,
                company=company_name,
                url=job_url,
                location=", ".join(location_parts),
                requirements=requirements,
            )
        )

    print(f"[deshaw] fetched {len(jobs)} jobs")
    return jobs


def fetch_hrt(
    company: str | None = None,
    *,
    careers_url: str = "https://www.hudsonrivertrading.com/careers/",
) -> list[dict[str, str]]:
    """Fetch Hudson River Trading roles via the WordPress ``get_hrt_jobs_handler`` AJAX API."""
    company_name = company or "Hudson River Trading"
    ajax_url = "https://www.hudsonrivertrading.com/wp-admin/admin-ajax.php"
    # Default board settings from the careers page ``data-filters-settings`` attribute.
    setting = (
        '{"meta_data":[{"icon":"","term":"locations"},'
        '{"icon":"","term":"job-category"},'
        '{"icon":"","term":"job-type"}],'
        '"settings":{"hide_job_id":true}}'
    )

    try:
        resp = requests.post(
            ajax_url,
            data=[
                ("action", "get_hrt_jobs_handler"),
                # Empty taxonomy arrays make the endpoint return []; omit them.
                ("data[search]", ""),
                ("setting", setting),
            ],
            headers={
                **_BROWSER_HEADERS,
                "Accept": "*/*",
                "Referer": careers_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=45,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[hrt] fetch failed: {exc}", file=sys.stderr)
        return []

    try:
        payload = resp.json()
    except ValueError as exc:
        print(f"[hrt] invalid JSON: {exc}", file=sys.stderr)
        return []

    if not isinstance(payload, list):
        print(f"[hrt] unexpected payload type {type(payload).__name__}", file=sys.stderr)
        return []

    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or ""
        title = unescape(str(item.get("title") or "")).strip()
        if not title:
            title_m = re.search(
                r'class="hrt-card-title"[^>]*>\s*([^<]+)', content, re.IGNORECASE
            )
            title = unescape(title_m.group(1)).strip() if title_m else ""
        if not title:
            continue

        # Prefer WordPress post ID: Greenhouse ``data-jobid`` can collide across roles.
        job_id = str(item.get("ID") or "").strip()
        if not job_id:
            job_id_m = re.search(r'data-jobid="([^"]+)"', content, re.IGNORECASE)
            job_id = job_id_m.group(1).strip() if job_id_m else ""
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)

        link_m = re.search(
            r'class="hrt-card-title"\s+href="([^"]+)"', content, re.IGNORECASE
        )
        url = unescape(link_m.group(1)).strip() if link_m else ""
        if not url:
            url = careers_url

        # Prefer desktop meta location list (first ul before second-list).
        loc_block = re.search(
            r'class="hrt-card-meta-desktop"[^>]*>\s*<ul class="hrt-card-info-list">(.*?)</ul>',
            content,
            re.DOTALL | re.IGNORECASE,
        )
        location_parts: list[str] = []
        if loc_block:
            for span in re.findall(r"<span>([^<]+)</span>", loc_block.group(1)):
                label = unescape(re.sub(r"\s+", " ", span)).strip()
                if label and label not in location_parts:
                    location_parts.append(label)

        desc_m = re.search(
            r'class="hrt-card-description"[^>]*>(.*?)</p>',
            content,
            re.DOTALL | re.IGNORECASE,
        )
        requirements = ""
        if desc_m:
            requirements = unescape(re.sub(r"<[^>]+>", " ", desc_m.group(1)))
            requirements = re.sub(r"\s+", " ", requirements).strip()
        if not requirements:
            requirements = unescape(str(item.get("description") or "")).strip()

        jobs.append(
            normalize_job(
                job_id=f"hrt:{job_id}",
                title=title,
                company=company_name,
                url=url,
                location=", ".join(location_parts),
                requirements=requirements,
            )
        )

    print(f"[hrt] fetched {len(jobs)} jobs")
    return jobs


def fetch_workable(
    account: str,
    company: str | None = None,
) -> list[dict[str, str]]:
    """Fetch jobs from a Workable public careers account (POST /api/v3/.../jobs)."""
    company_name = company or account
    api_url = f"https://apply.workable.com/api/v3/accounts/{account}/jobs"
    try:
        resp = requests.post(
            api_url,
            headers={
                **_BROWSER_HEADERS,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "query": "",
                "location": [],
                "department": [],
                "worktype": [],
                "workplace": [],
            },
            timeout=45,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[workable:{account}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    results = payload.get("results") or []
    jobs: list[dict[str, str]] = []

    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("state") and str(item.get("state")).lower() not in {"published", "live", ""}:
            # Still include when state is missing; skip clearly non-public.
            if str(item.get("state")).lower() in {"draft", "archived", "closed"}:
                continue
        shortcode = str(item.get("shortcode") or item.get("id") or "")
        if not shortcode:
            continue

        location = ""
        loc = item.get("location")
        if isinstance(loc, dict):
            parts = [loc.get("city") or "", loc.get("region") or "", loc.get("country") or ""]
            location = ", ".join(p for p in parts if p)
        locs = item.get("locations") or []
        if isinstance(locs, list) and locs:
            joined: list[str] = []
            for loc in locs:
                if not isinstance(loc, dict):
                    continue
                parts = [loc.get("city") or "", loc.get("region") or "", loc.get("country") or ""]
                label = ", ".join(p for p in parts if p)
                if label:
                    joined.append(label)
            if joined:
                location = "; ".join(joined)

        if item.get("remote") or (item.get("workplace") or "").lower() == "remote":
            if location and "remote" not in location.lower():
                location = f"{location} (Remote)"
            elif not location:
                location = "Remote"

        jobs.append(
            normalize_job(
                job_id=f"workable:{account}:{shortcode}",
                title=(item.get("title") or "").strip() or "Untitled",
                company=company_name,
                url=f"https://apply.workable.com/{account}/j/{shortcode}/",
                location=location,
            )
        )

    print(f"[workable:{account}] fetched {len(jobs)} jobs")
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


_GOLDMAN_GRAPHQL_URL = "https://api-higher.gs.com/gateway/api/v1/graphql"
_GOLDMAN_ROLE_SEARCH_QUERY = """
query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    page { pageNumber pageSize hasNext }
    items {
      roleId
      jobTitle
      corporateTitle
      division
      jobFunction
      shortDescription
      locations { city state country primary }
    }
  }
}
""".strip()


def _goldman_format_location(locs: Any) -> str:
    """Join Goldman Higher location objects into a display string."""
    if not isinstance(locs, list) or not locs:
        return ""
    parts: list[str] = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        bits = [loc.get("city") or "", loc.get("state") or "", loc.get("country") or ""]
        label = ", ".join(b for b in bits if b)
        if label and label not in parts:
            parts.append(label)
    return " | ".join(parts)


def fetch_goldman(
    company: str | None = None,
    *,
    graphql_url: str = _GOLDMAN_GRAPHQL_URL,
    page_size: int = 50,
    experiences: list[str] | None = None,
) -> list[dict[str, str]]:
    """Fetch roles from Goldman Sachs Higher GraphQL (api-higher.gs.com)."""
    company_name = company or "Goldman Sachs"
    exp = experiences or ["PROFESSIONAL", "EARLY_CAREER"]
    jobs: list[dict[str, str]] = []
    page_number = 1
    total: int | None = None

    while True:
        headers = {
            **_BROWSER_HEADERS,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://higher.gs.com",
            "Referer": "https://higher.gs.com/results",
            "x-higher-request-id": str(uuid.uuid4()),
            "x-higher-session-id": str(uuid.uuid4()),
        }
        variables = {
            "searchQueryInput": {
                "page": {"pageNumber": page_number, "pageSize": page_size},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"},
                "experiences": exp,
            }
        }
        try:
            resp = requests.post(
                graphql_url,
                headers=headers,
                json={
                    "operationName": "GetRoles",
                    "query": _GOLDMAN_ROLE_SEARCH_QUERY,
                    "variables": variables,
                },
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[goldman] fetch failed at page={page_number}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[goldman] invalid JSON at page={page_number}: {exc}", file=sys.stderr)
            break

        if payload.get("errors"):
            print(f"[goldman] GraphQL errors at page={page_number}: {payload['errors']}", file=sys.stderr)
            break

        data = (payload.get("data") or {}).get("roleSearch") or {}
        if total is None:
            try:
                total = int(data.get("totalCount")) if data.get("totalCount") is not None else None
            except (TypeError, ValueError):
                total = None

        items = data.get("items") or []
        if not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue
            role_id = str(item.get("roleId") or "").strip()
            title = (item.get("jobTitle") or "").strip()
            if not role_id or not title:
                continue
            desc = unescape(str(item.get("shortDescription") or ""))
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            jobs.append(
                normalize_job(
                    job_id=f"goldman:{role_id}",
                    title=title,
                    company=company_name,
                    url=f"https://higher.gs.com/roles/{role_id}",
                    location=_goldman_format_location(item.get("locations")),
                    requirements=desc,
                )
            )

        page_meta = data.get("page") or {}
        has_next = bool(page_meta.get("hasNext"))
        if not has_next:
            break
        if total is not None and len(jobs) >= total:
            break
        page_number += 1
        if page_number > 500:
            break

    print(f"[goldman] fetched {len(jobs)} jobs")
    return jobs


def fetch_oracle_cloud(
    host: str,
    site_number: str,
    company: str | None = None,
    *,
    page_size: int = 200,
) -> list[dict[str, str]]:
    """Fetch jobs from an Oracle Cloud HCM Candidate Experience board (e.g. JPMorgan)."""
    host = host.replace("https://", "").replace("http://", "").strip("/")
    company_name = company or host
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    offset = 0
    total: int | None = None
    # Finder separators must stay unencoded (; = ,) or Oracle returns empty/errors.
    base = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&expand=requisitionList"
        f"&finder=findReqs;siteNumber={site_number},limit={int(page_size)},offset="
    )
    empty_streak = 0

    while True:
        try:
            resp = requests.get(
                f"{base}{offset}",
                headers={**_BROWSER_HEADERS, "Accept": "application/json"},
                timeout=60,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(
                f"[oracle_cloud:{site_number}] fetch failed at offset={offset}: {exc}",
                file=sys.stderr,
            )
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(
                f"[oracle_cloud:{site_number}] invalid JSON at offset={offset}: {exc}",
                file=sys.stderr,
            )
            break

        top = (payload.get("items") or [None])[0]
        if not isinstance(top, dict):
            break

        if total is None:
            try:
                total = int(top["TotalJobsCount"]) if top.get("TotalJobsCount") is not None else None
            except (TypeError, ValueError):
                total = None

        requisitions = top.get("requisitionList") or []
        if not isinstance(requisitions, list) or not requisitions:
            empty_streak += 1
            if empty_streak >= 2:
                break
            offset += page_size
            if total is not None and offset >= total:
                break
            continue
        empty_streak = 0

        for item in requisitions:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("Id") or "").strip()
            title = (item.get("Title") or "").strip()
            if not job_id or not title or job_id in seen:
                continue
            seen.add(job_id)
            location = (item.get("PrimaryLocation") or "").strip()
            workplace = (item.get("WorkplaceType") or "").strip()
            if workplace and workplace.lower() not in location.lower():
                location = f"{location} ({workplace})" if location else workplace
            reqs = (item.get("ShortDescriptionStr") or "").strip()
            jobs.append(
                normalize_job(
                    job_id=f"oracle_cloud:{site_number}:{job_id}",
                    title=title,
                    company=company_name,
                    url=(
                        f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                        f"{site_number}/job/{job_id}"
                    ),
                    location=location,
                    requirements=reqs,
                )
            )

        # Advance by requested page size — Oracle may return fewer than `limit`
        # mid-stream without being finished.
        offset += page_size
        if total is not None and offset >= total:
            break
        if offset > 20000:
            break

    print(f"[oracle_cloud:{site_number}] fetched {len(jobs)} jobs")
    return jobs


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
                fetched = fetch_greenhouse(
                    token,
                    company=company or token,
                    metadata_includes=site.get("metadata_includes") or None,
                )
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
            elif site_type == "workable":
                account = site.get("account") or site.get("board_token") or site.get("site")
                if not account:
                    continue
                fetched = fetch_workable(account, company=company or account)
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
            elif site_type == "citadel":
                fetched = fetch_citadel(
                    company=company or "Citadel",
                    url=site.get("url")
                    or "https://www.citadel.com/careers/open-opportunities/",
                )
            elif site_type == "twosigma":
                fetched = fetch_twosigma(
                    company=company or "Two Sigma",
                    search_url=site.get("search_url")
                    or site.get("url")
                    or "https://careers.twosigma.com/careers/OpenRoles/",
                )
            elif site_type == "deshaw":
                fetched = fetch_deshaw(
                    company=company or "D. E. Shaw",
                    url=site.get("url") or "https://www.deshaw.com/careers",
                )
            elif site_type == "hrt":
                fetched = fetch_hrt(
                    company=company or "Hudson River Trading",
                    careers_url=site.get("url")
                    or "https://www.hudsonrivertrading.com/careers/",
                )
            elif site_type == "goldman":
                fetched = fetch_goldman(
                    company=company or "Goldman Sachs",
                    graphql_url=site.get("graphql_url") or _GOLDMAN_GRAPHQL_URL,
                    experiences=site.get("experiences") or None,
                )
            elif site_type == "oracle_cloud":
                host = site.get("host")
                site_number = site.get("site_number") or site.get("siteNumber")
                if not host and site.get("url"):
                    host = urlparse(site["url"]).netloc
                if not host or not site_number:
                    continue
                fetched = fetch_oracle_cloud(
                    host,
                    str(site_number),
                    company=company or host,
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


def filter_unseen(jobs: list[dict[str, str]], seen: dict[str, Any]) -> list[dict[str, str]]:
    return [job for job in jobs if job["id"] not in seen]


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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError) and exc.code == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "too many requests" in text


def _parse_retry_delay_seconds(exc: BaseException) -> float | None:
    """Extract server-suggested wait from Gemini RetryInfo / error message."""
    details = getattr(exc, "details", None)
    candidates: list[Any] = []
    if isinstance(details, dict):
        err = details.get("error") if isinstance(details.get("error"), dict) else details
        if isinstance(err, dict):
            nested = err.get("details")
            if isinstance(nested, list):
                candidates.extend(nested)
            msg = err.get("message")
            if isinstance(msg, str):
                candidates.append({"message": msg})
    candidates.append({"message": str(exc)})

    for item in candidates:
        if isinstance(item, dict):
            delay = item.get("retryDelay")
            if isinstance(delay, str):
                m = re.fullmatch(r"(\d+(?:\.\d+)?)s?", delay.strip())
                if m:
                    return float(m.group(1))
            if isinstance(delay, (int, float)):
                return float(delay)
            msg = item.get("message")
            if isinstance(msg, str):
                m = re.search(r"retry in\s+(\d+(?:\.\d+)?)\s*s", msg, re.I)
                if m:
                    return float(m.group(1))
    return None


def retry_sleep_seconds(exc: BaseException, attempt: int) -> float:
    """Prefer API retryDelay; otherwise exponential backoff with a sane ceiling."""
    suggested = _parse_retry_delay_seconds(exc)
    if suggested is not None:
        return min(max(suggested, 1.0) + 1.0, 120.0)
    return min(2**attempt, 60.0)


def parse_models() -> list[str]:
    """Resolve Gemini model list from GEMINI_MODELS or GEMINI_MODEL."""
    raw = os.environ.get("GEMINI_MODELS") or os.environ.get("GEMINI_MODEL") or ""
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or list(DEFAULT_MODELS)


def match_job(
    job: dict[str, str],
    resume_lines: list[str],
    client: genai.Client,
    *,
    models: list[str],
    location_requirement: str = "US",
    max_retries: int = DEFAULT_MAX_RETRIES,
    model_start_index: int = 0,
) -> MatchResult:
    if not models:
        raise ValueError("models must not be empty")

    user_prompt = (
        f"Location requirement: {location_requirement}\n\n"
        f"Job title: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Listed location: {job.get('location', '')}\n"
        f"URL: {job.get('url', '')}\n\n"
        f"Requirements section:\n{job.get('requirements') or '(empty)'}\n\n"
        f"Resume lines:\n" + "\n".join(f"- {line}" for line in resume_lines)
    )

    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        model = models[(model_start_index + attempt - 1) % len(models)]
        try:
            response = client.models.generate_content(
                model=model,
                contents=f"{SYSTEM_PROMPT}\n\n{user_prompt}",
                config={
                    "response_mime_type": "application/json",
                    "response_schema": MatchResult,
                },
            )
            break
        except Exception as exc:  # noqa: BLE001 — classify rate limits vs hard failures
            if is_rate_limit_error(exc) and attempt < attempts:
                next_model = models[(model_start_index + attempt) % len(models)]
                # Quotas are per-model: finish a full model cycle before sleeping.
                if len(models) > 1 and (attempt % len(models)) != 0:
                    print(
                        f"[match] 429 on {model} for {job.get('id')}; "
                        f"trying {next_model} ({attempt}/{attempts})",
                        file=sys.stderr,
                    )
                    continue
                delay = retry_sleep_seconds(exc, attempt)
                print(
                    f"[match] 429 on {model} for {job.get('id')}; "
                    f"retry {attempt}/{attempts} in {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
    else:
        raise RuntimeError(f"match_job exhausted retries for {job.get('id')}")

    raw = (response.text or "").strip()
    if not raw:
        return MatchResult(
            is_match=False,
            missing_requirements=["LLM returned no structured output"],
            reason="Failed to parse structured match result.",
        )
    return MatchResult.model_validate_json(raw)


def write_candidates_summary(jobs: list[dict[str, str]], *, unseen_count: int) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## Deterministic filter candidates",
        "",
        f"_Survived deterministic filters: **{len(jobs)}** "
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


def write_github_summary(matches: list[dict[str, Any]]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## Job matches",
        "",
    ]
    if not matches:
        lines.append("_No matches today._")
    else:
        lines.extend(
            [
                "| Title | Company | Location | URL | Reason |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for m in matches:
            title = (m.get("title") or "").replace("|", "\\|")
            company = (m.get("company") or "").replace("|", "\\|")
            location = (m.get("location") or "").replace("|", "\\|")
            url = m.get("url") or ""
            reason = (m.get("reason") or "").replace("|", "\\|").replace("\n", " ")
            link = f"[link]({url})" if url else ""
            lines.append(f"| {title} | {company} | {location} | {link} | {reason} |")

    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch jobs, apply deterministic filters, then score with Gemini."
    )
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help=(
            "Stop after title/location filters; write candidates.json and skip Gemini "
            "and seen_jobs.json updates."
        ),
    )
    return parser.parse_args(argv)


def run_deterministic_only(config: dict[str, Any], seen: dict[str, Any]) -> int:
    clear_candidates()
    candidates = fetch_jobs(config)
    unseen = filter_unseen(candidates, seen)

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
    write_candidates_summary(candidates, unseen_count=len(unseen))
    print(f"\nWrote {len(candidates)} candidates to {CANDIDATES_PATH.name}")
    return 0


def run_llm_match(config: dict[str, Any], seen: dict[str, Any]) -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is required (set in .env or environment)", file=sys.stderr)
        return 1

    models = parse_models()
    request_interval = _env_float("GEMINI_REQUEST_INTERVAL_SEC", DEFAULT_REQUEST_INTERVAL_SEC)
    max_retries = _env_int("GEMINI_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    max_jobs = _env_int("GEMINI_MAX_JOBS_PER_RUN", DEFAULT_MAX_JOBS_PER_RUN)
    resume_lines = load_resume()
    if not resume_lines:
        print("ERROR: resume.txt has no usable experience lines", file=sys.stderr)
        return 1

    all_jobs = fetch_jobs(config)
    unseen = filter_unseen(all_jobs, seen)
    print(f"[models] {', '.join(models)}")
    if max_jobs > 0 and len(unseen) > max_jobs:
        print(
            f"Fetched {len(all_jobs)} jobs; {len(unseen)} unseen; "
            f"scoring {max_jobs} this run (GEMINI_MAX_JOBS_PER_RUN)"
        )
        unseen = unseen[:max_jobs]
    else:
        print(f"Fetched {len(all_jobs)} jobs; {len(unseen)} unseen")

    client = genai.Client(api_key=api_key)
    location_requirement = config.get("location_requirement") or "US"
    matches: list[dict[str, Any]] = []
    now = utc_now_iso()
    stopped_for_rate_limit = False

    for index, job in enumerate(unseen):
        if index > 0 and request_interval > 0:
            time.sleep(request_interval)

        try:
            result = match_job(
                job,
                resume_lines,
                client,
                models=models,
                location_requirement=location_requirement,
                max_retries=max_retries,
                model_start_index=index % len(models),
            )
        except Exception as exc:  # noqa: BLE001 — keep run going on single-job failures
            print(f"[match] failed for {job.get('id')}: {exc}", file=sys.stderr)
            if is_rate_limit_error(exc):
                # Leave this job (and the rest) unseen so the next run can retry.
                stopped_for_rate_limit = True
                print(
                    "[match] rate limited after retries; "
                    "leaving remaining jobs unseen for the next run",
                    file=sys.stderr,
                )
                break
            result = MatchResult(
                is_match=False,
                missing_requirements=["matcher_error"],
                reason=f"Matcher error: {exc}",
            )

        # Record every LLM-evaluated job (match or not) so it is never re-scored.
        seen[job["id"]] = {
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "url": job.get("url", ""),
            "matched": bool(result.is_match),
            "seen_at": now,
        }
        # Persist immediately so a mid-run crash/timeout does not re-send these.
        save_seen(seen)

        status = "MATCH" if result.is_match else "skip"
        print(f"[{status}] {job.get('company')} — {job.get('title')}: {result.reason}")

        if result.is_match:
            matches.append(
                {
                    "title": job.get("title", ""),
                    "company": job.get("company", ""),
                    "location": job.get("location", ""),
                    "url": job.get("url", ""),
                    "reason": result.reason,
                    "missing_requirements": result.missing_requirements,
                }
            )

    write_github_summary(matches)
    if stopped_for_rate_limit:
        print(
            f"Stopped early due to rate limits. {len(matches)} match(es). "
            f"Updated {SEEN_PATH.name}."
        )
        return 0
    print(f"Done. {len(matches)} match(es). Updated {SEEN_PATH.name}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    seen = load_seen()
    if args.deterministic_only:
        return run_deterministic_only(config, seen)
    return run_llm_match(config, seen)


if __name__ == "__main__":
    raise SystemExit(main())
