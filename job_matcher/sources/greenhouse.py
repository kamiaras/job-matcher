"""Greenhouse public boards API."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job

def fetch_greenhouse(
    board_token: str,
    company: str | None = None,
    *,
    metadata_includes: list[str] | None = None,
) -> list[dict[str, str]]:
    """Fetch jobs from Greenhouse public boards API.

    ``metadata_includes`` optionally keeps only jobs whose Greenhouse metadata
    values contain any of the given needles (e.g. W&B roles on CoreWeave's board).
    """
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    try:
        resp = requests.get(api_url, params={"content": "true"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[greenhouse:{board_token}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    jobs_raw = payload.get("jobs") or []
    company_name = company or board_token
    needles = [n.lower() for n in (metadata_includes or []) if n and str(n).strip()]
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        if needles:
            meta_parts: list[str] = []
            for meta in item.get("metadata") or []:
                if isinstance(meta, dict) and meta.get("value") is not None:
                    meta_parts.append(str(meta["value"]))
            meta_blob = " ".join(meta_parts).lower()
            if not any(n in meta_blob for n in needles):
                continue

        job_id = str(item.get("id") or item.get("absolute_url") or "")
        if not job_id:
            continue
        location = ""
        loc = item.get("location")
        if isinstance(loc, dict):
            location = loc.get("name") or ""
        elif isinstance(loc, str):
            location = loc

        # Greenhouse sometimes embeds HTML content; keep as requirements text.
        requirements = (item.get("content") or item.get("description") or "").strip()
        jobs.append(
            normalize_job(
                job_id=f"greenhouse:{board_token}:{job_id}",
                title=(item.get("title") or "").strip() or "Untitled",
                company=company_name,
                url=(item.get("absolute_url") or "").strip(),
                location=location,
                requirements=requirements,
            )
        )
    print(f"[greenhouse:{board_token}] fetched {len(jobs)} jobs")
    return jobs
