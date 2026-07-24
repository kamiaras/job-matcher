"""Amazon.jobs public search JSON API."""

from __future__ import annotations

import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

def fetch_amazon(
    company: str | None = None,
    *,
    page_size: int = 100,
) -> list[dict[str, str]]:
    """Fetch jobs from Amazon.jobs public search JSON API."""
    company_name = company or "Amazon"
    api_url = "https://www.amazon.jobs/en/search.json"
    jobs: list[dict[str, str]] = []
    offset = 0
    total: int | None = None

    while True:
        try:
            resp = requests.get(
                api_url,
                params={"offset": offset, "result_limit": page_size, "sort": "relevant"},
                headers={**_BROWSER_HEADERS, "Accept": "application/json"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[amazon] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[amazon] invalid JSON at offset={offset}: {exc}", file=sys.stderr)
            break

        if total is None:
            raw_hits = payload.get("hits")
            try:
                total = int(raw_hits) if raw_hits is not None else None
            except (TypeError, ValueError):
                total = None

        results = payload.get("jobs") or []
        if not results:
            break

        for item in results:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id_icims") or item.get("id") or "").strip()
            path = (item.get("job_path") or "").strip()
            if not job_id and path:
                job_id = path
            if not job_id:
                continue
            url = f"https://www.amazon.jobs{path}" if path else ""
            location = (
                (item.get("normalized_location") or item.get("location") or "").strip()
            )
            requirements = "\n\n".join(
                part
                for part in (
                    (item.get("basic_qualifications") or "").strip(),
                    (item.get("preferred_qualifications") or "").strip(),
                    (item.get("description") or "").strip(),
                )
                if part
            )
            jobs.append(
                normalize_job(
                    job_id=f"amazon:{job_id}",
                    title=(item.get("title") or "").strip() or "Untitled",
                    company=(item.get("company_name") or "").strip() or company_name,
                    url=url,
                    location=location,
                    requirements=requirements,
                )
            )

        offset += len(results)
        if total is not None and offset >= total:
            break
        if len(results) < page_size:
            break
        if offset > 15_000:
            print("[amazon] abort: pagination exceeded 15000", file=sys.stderr)
            break

    print(f"[amazon] fetched {len(jobs)} jobs")
    return jobs
