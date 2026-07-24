"""Ashby public job posting API."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job

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
