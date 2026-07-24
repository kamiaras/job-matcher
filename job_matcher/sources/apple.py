"""Apple Careers SSR hydration listings."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

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
