"""Microsoft Careers Eightfold PCSX search API."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

def fetch_microsoft(
    company: str | None = None,
    *,
    domain: str = "microsoft.com",
    host: str = "apply.careers.microsoft.com",
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch jobs from Microsoft Careers Eightfold PCSX search API."""
    company_name = company or "Microsoft"
    api_url = f"https://{host}/api/pcsx/search"
    jobs: list[dict[str, str]] = []
    start = 0
    total: int | None = None

    while True:
        try:
            resp = requests.get(
                api_url,
                params={"domain": domain, "start": start, "num": page_size},
                headers={
                    **_BROWSER_HEADERS,
                    "Accept": "application/json",
                    "Referer": f"https://{host}/careers/jobs",
                },
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[microsoft] fetch failed at start={start}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[microsoft] invalid JSON at start={start}: {exc}", file=sys.stderr)
            break

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            print(f"[microsoft] unexpected payload at start={start}", file=sys.stderr)
            break

        if total is None:
            raw_count = data.get("count")
            try:
                total = int(raw_count) if raw_count is not None else None
            except (TypeError, ValueError):
                total = None

        positions = data.get("positions") or []
        if not positions:
            break

        for item in positions:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id") or item.get("displayJobId") or "").strip()
            if not job_id:
                continue
            locs = item.get("locations") or item.get("standardizedLocations") or []
            if isinstance(locs, list):
                location = ", ".join(str(x) for x in locs if x)
            else:
                location = str(locs or "").strip()
            path = (item.get("positionUrl") or "").strip()
            url = f"https://{host}{path}" if path.startswith("/") else path
            if not url:
                url = f"https://{host}/careers/job/{job_id}"
            jobs.append(
                normalize_job(
                    job_id=f"microsoft:{job_id}",
                    title=(item.get("name") or "").strip() or "Untitled",
                    company=company_name,
                    url=url,
                    location=location,
                )
            )

        start += len(positions)
        if total is not None and start >= total:
            break
        # PCSX often ignores larger page sizes and returns a fixed ~10.
        if len(positions) < 1:
            break
        if start > 20_000:
            print("[microsoft] abort: pagination exceeded 20000", file=sys.stderr)
            break

    print(f"[microsoft] fetched {len(jobs)} jobs")
    return jobs
