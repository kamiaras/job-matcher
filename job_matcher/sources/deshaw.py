"""D. E. Shaw careers __NEXT_DATA__ listings."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

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
