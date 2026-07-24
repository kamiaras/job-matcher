"""Lever public postings API."""

from __future__ import annotations

import sys
from typing import Any

import requests

from job_matcher.normalize import normalize_job

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
