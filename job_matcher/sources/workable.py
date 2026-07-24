"""Workable public careers account API."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

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
