"""Workday CXS public careers boards."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

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
