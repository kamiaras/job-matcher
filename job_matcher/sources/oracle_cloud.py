"""Oracle Cloud HCM Candidate Experience boards."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

def fetch_oracle_cloud(
    host: str,
    site_number: str,
    company: str | None = None,
    *,
    page_size: int = 200,
) -> list[dict[str, str]]:
    """Fetch jobs from an Oracle Cloud HCM Candidate Experience board (e.g. JPMorgan)."""
    host = host.replace("https://", "").replace("http://", "").strip("/")
    company_name = company or host
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    offset = 0
    total: int | None = None
    # Finder separators must stay unencoded (; = ,) or Oracle returns empty/errors.
    base = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&expand=requisitionList"
        f"&finder=findReqs;siteNumber={site_number},limit={int(page_size)},offset="
    )
    empty_streak = 0

    while True:
        try:
            resp = requests.get(
                f"{base}{offset}",
                headers={**_BROWSER_HEADERS, "Accept": "application/json"},
                timeout=60,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(
                f"[oracle_cloud:{site_number}] fetch failed at offset={offset}: {exc}",
                file=sys.stderr,
            )
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(
                f"[oracle_cloud:{site_number}] invalid JSON at offset={offset}: {exc}",
                file=sys.stderr,
            )
            break

        top = (payload.get("items") or [None])[0]
        if not isinstance(top, dict):
            break

        if total is None:
            try:
                total = int(top["TotalJobsCount"]) if top.get("TotalJobsCount") is not None else None
            except (TypeError, ValueError):
                total = None

        requisitions = top.get("requisitionList") or []
        if not isinstance(requisitions, list) or not requisitions:
            empty_streak += 1
            if empty_streak >= 2:
                break
            offset += page_size
            if total is not None and offset >= total:
                break
            continue
        empty_streak = 0

        for item in requisitions:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("Id") or "").strip()
            title = (item.get("Title") or "").strip()
            if not job_id or not title or job_id in seen:
                continue
            seen.add(job_id)
            location = (item.get("PrimaryLocation") or "").strip()
            workplace = (item.get("WorkplaceType") or "").strip()
            if workplace and workplace.lower() not in location.lower():
                location = f"{location} ({workplace})" if location else workplace
            reqs = (item.get("ShortDescriptionStr") or "").strip()
            jobs.append(
                normalize_job(
                    job_id=f"oracle_cloud:{site_number}:{job_id}",
                    title=title,
                    company=company_name,
                    url=(
                        f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                        f"{site_number}/job/{job_id}"
                    ),
                    location=location,
                    requirements=reqs,
                )
            )

        # Advance by requested page size — Oracle may return fewer than `limit`
        # mid-stream without being finished.
        offset += page_size
        if total is not None and offset >= total:
            break
        if offset > 20000:
            break

    print(f"[oracle_cloud:{site_number}] fetched {len(jobs)} jobs")
    return jobs
