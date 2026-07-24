"""Eightfold public careers API."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job

def fetch_eightfold(
    domain: str,
    host: str,
    company: str | None = None,
    *,
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch jobs from an Eightfold public careers API (e.g. Netflix).

    List endpoint returns title/location/url; descriptions are empty unless
    each position is fetched individually (skipped here — filters are title-only).
    """
    api_url = f"https://{host}/api/apply/v2/jobs"
    company_name = company or domain
    jobs: list[dict[str, str]] = []
    start = 0
    total: int | None = None

    while True:
        try:
            resp = requests.get(
                api_url,
                params={"domain": domain, "start": start, "num": page_size},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[eightfold:{domain}] fetch failed at start={start}: {exc}", file=sys.stderr)
            break

        payload = resp.json()
        if total is None:
            raw_count = payload.get("count")
            total = int(raw_count) if raw_count is not None else None

        positions = payload.get("positions") or []
        if not positions:
            break

        for item in positions:
            if not isinstance(item, dict) or item.get("isPrivate"):
                continue
            job_id = str(item.get("id") or "")
            if not job_id:
                continue

            locs = item.get("locations") or []
            if isinstance(locs, list) and locs:
                location = ", ".join(str(x) for x in locs if x)
            else:
                location = (item.get("location") or "").strip()

            url = (item.get("canonicalPositionUrl") or "").strip()
            if not url:
                url = f"https://{host}/careers/job/{job_id}"

            jobs.append(
                normalize_job(
                    job_id=f"eightfold:{domain}:{job_id}",
                    title=(item.get("name") or item.get("posting_name") or "").strip()
                    or "Untitled",
                    company=company_name,
                    url=url,
                    location=location,
                    requirements=(item.get("job_description") or "").strip(),
                )
            )

        start += len(positions)
        if total is not None and start >= total:
            break
        # Eightfold caps page size (~10); stop if a short page arrives early.
        if len(positions) < page_size:
            break
        if start > 10_000:
            print(f"[eightfold:{domain}] abort: pagination exceeded 10000", file=sys.stderr)
            break

    print(f"[eightfold:{domain}] fetched {len(jobs)} jobs")
    return jobs
