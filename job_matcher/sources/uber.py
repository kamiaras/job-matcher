"""Uber first-party careers search API."""

from __future__ import annotations

import sys
from typing import Any

import requests

from job_matcher.normalize import normalize_job

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
