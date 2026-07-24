"""LinkedIn company jobs via jobs-guest HTML API."""

from __future__ import annotations

import re
import sys

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

def fetch_linkedin(
    company: str | None = None,
    *,
    company_ids: str = "1337",
    page_size: int = 10,
) -> list[dict[str, str]]:
    """Fetch LinkedIn company jobs via the public jobs-guest HTML API."""
    company_name = company or "LinkedIn"
    api_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    start = 0
    empty_streak = 0

    while start < 2_000:
        try:
            resp = requests.get(
                api_url,
                params={"f_C": company_ids, "start": start, "geoId": "92000000"},
                headers={**_BROWSER_HEADERS, "Accept": "text/html"},
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[linkedin] fetch failed at start={start}: {exc}", file=sys.stderr)
            break

        html = resp.text
        ids = re.findall(r'data-entity-urn="urn:li:jobPosting:(\d+)"', html)
        titles = [
            re.sub(r"\s+", " ", t).strip()
            for t in re.findall(
                r'class="[^"]*base-search-card__title[^"]*"[^>]*>\s*([^<]+)', html
            )
        ]
        locs = [
            re.sub(r"\s+", " ", t).strip()
            for t in re.findall(
                r'class="[^"]*job-search-card__location[^"]*"[^>]*>\s*([^<]+)', html
            )
        ]
        hrefs = re.findall(
            r'href="(https://(?:www\.)?linkedin\.com/jobs/view/[^"?]+)', html
        )

        if not ids:
            break

        new_on_page = 0
        for i, job_id in enumerate(ids):
            if job_id in seen:
                continue
            seen.add(job_id)
            new_on_page += 1
            title = titles[i] if i < len(titles) else "Untitled"
            location = locs[i] if i < len(locs) else ""
            url = hrefs[i] if i < len(hrefs) else f"https://www.linkedin.com/jobs/view/{job_id}"
            jobs.append(
                normalize_job(
                    job_id=f"linkedin:{job_id}",
                    title=title or "Untitled",
                    company=company_name,
                    url=url,
                    location=location,
                )
            )

        if new_on_page == 0:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0

        start += max(len(ids), page_size)

    print(f"[linkedin] fetched {len(jobs)} jobs")
    return jobs
