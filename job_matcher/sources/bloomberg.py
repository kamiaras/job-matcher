"""Bloomberg Avature SearchJobs HTML listings."""

from __future__ import annotations

import re
import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

def fetch_bloomberg(
    company: str | None = None,
    *,
    search_url: str = "https://bloomberg.avature.net/careers/SearchJobs/",
    page_size: int = 12,
) -> list[dict[str, str]]:
    """Fetch jobs from Bloomberg Avature SearchJobs HTML (jobOffset pagination)."""
    company_name = company or "Bloomberg"
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    offset = 0
    max_offset = 5_000

    while offset <= max_offset:
        try:
            resp = requests.get(
                search_url,
                params={
                    "listFilterMode": 1,
                    "jobRecordsPerPage": page_size,
                    "jobOffset": offset,
                },
                headers={**_BROWSER_HEADERS, "Accept": "text/html"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[bloomberg] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        # Title+url pairs from JobDetail anchors (skip bare "Apply" CTAs).
        pairs = re.findall(
            r'href="(https://bloomberg\.avature\.net/careers/JobDetail/[^"]+/(\d+))"[^>]*>\s*([^<]+)',
            resp.text,
        )
        new_on_page = 0
        for url, job_id, title in pairs:
            clean_title = re.sub(r"\s+", " ", title).strip()
            if not clean_title or clean_title.lower() == "apply":
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            new_on_page += 1
            jobs.append(
                normalize_job(
                    job_id=f"bloomberg:{job_id}",
                    title=clean_title,
                    company=company_name,
                    url=url,
                )
            )

        if new_on_page == 0:
            break
        offset += page_size

    print(f"[bloomberg] fetched {len(jobs)} jobs")
    return jobs
