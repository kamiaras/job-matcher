"""Google Careers HTML-embedded job listings."""

from __future__ import annotations

import json
import sys
from typing import Any

import requests

from job_matcher.normalize import normalize_job

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
