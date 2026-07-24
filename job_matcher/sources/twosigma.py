"""Two Sigma Avature OpenRoles HTML listings."""

from __future__ import annotations

import re
import sys
from html import unescape

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

def fetch_twosigma(
    company: str | None = None,
    *,
    search_url: str = "https://careers.twosigma.com/careers/OpenRoles/",
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch Two Sigma roles from the Avature OpenRoles HTML listing."""
    company_name = company or "Two Sigma"
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
            print(f"[twosigma] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        articles = re.findall(r"<article[^>]*>(.*?)</article>", resp.text, re.DOTALL | re.IGNORECASE)
        new_on_page = 0
        for article in articles:
            link_m = re.search(
                r'href="(https://careers\.twosigma\.com/careers/JobDetail/[^"]+/(\d+))"\s*>\s*([^<]+?)\s*</a>',
                article,
            )
            if not link_m:
                continue
            url, job_id, title = link_m.group(1), link_m.group(2), link_m.group(3)
            clean_title = unescape(re.sub(r"\s+", " ", title)).strip()
            if not clean_title or clean_title.lower() == "view role":
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            new_on_page += 1

            loc_m = re.search(
                r'class="paragraph_inner-span"[^>]*>\s*([^<]+)',
                article,
                re.IGNORECASE,
            )
            location = re.sub(r"\s+", " ", loc_m.group(1)).strip() if loc_m else ""
            jobs.append(
                normalize_job(
                    job_id=f"twosigma:{job_id}",
                    title=clean_title,
                    company=company_name,
                    url=url,
                    location=location,
                )
            )

        if new_on_page == 0:
            break
        offset += page_size

    print(f"[twosigma] fetched {len(jobs)} jobs")
    return jobs
