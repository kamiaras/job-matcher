"""Meta Careers via sitemap + JSON-LD detail pages."""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import curl_requests

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
