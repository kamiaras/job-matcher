"""iCIMS-backed public JSON careers API."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

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
