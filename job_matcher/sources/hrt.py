"""Hudson River Trading WordPress AJAX job listings."""

from __future__ import annotations

import re
import sys
from html import unescape

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

def fetch_hrt(
    company: str | None = None,
    *,
    careers_url: str = "https://www.hudsonrivertrading.com/careers/",
) -> list[dict[str, str]]:
    """Fetch Hudson River Trading roles via the WordPress ``get_hrt_jobs_handler`` AJAX API."""
    company_name = company or "Hudson River Trading"
    ajax_url = "https://www.hudsonrivertrading.com/wp-admin/admin-ajax.php"
    # Default board settings from the careers page ``data-filters-settings`` attribute.
    setting = (
        '{"meta_data":[{"icon":"","term":"locations"},'
        '{"icon":"","term":"job-category"},'
        '{"icon":"","term":"job-type"}],'
        '"settings":{"hide_job_id":true}}'
    )

    try:
        resp = requests.post(
            ajax_url,
            data=[
                ("action", "get_hrt_jobs_handler"),
                # Empty taxonomy arrays make the endpoint return []; omit them.
                ("data[search]", ""),
                ("setting", setting),
            ],
            headers={
                **_BROWSER_HEADERS,
                "Accept": "*/*",
                "Referer": careers_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=45,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[hrt] fetch failed: {exc}", file=sys.stderr)
        return []

    try:
        payload = resp.json()
    except ValueError as exc:
        print(f"[hrt] invalid JSON: {exc}", file=sys.stderr)
        return []

    if not isinstance(payload, list):
        print(f"[hrt] unexpected payload type {type(payload).__name__}", file=sys.stderr)
        return []

    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or ""
        title = unescape(str(item.get("title") or "")).strip()
        if not title:
            title_m = re.search(
                r'class="hrt-card-title"[^>]*>\s*([^<]+)', content, re.IGNORECASE
            )
            title = unescape(title_m.group(1)).strip() if title_m else ""
        if not title:
            continue

        # Prefer WordPress post ID: Greenhouse ``data-jobid`` can collide across roles.
        job_id = str(item.get("ID") or "").strip()
        if not job_id:
            job_id_m = re.search(r'data-jobid="([^"]+)"', content, re.IGNORECASE)
            job_id = job_id_m.group(1).strip() if job_id_m else ""
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)

        link_m = re.search(
            r'class="hrt-card-title"\s+href="([^"]+)"', content, re.IGNORECASE
        )
        url = unescape(link_m.group(1)).strip() if link_m else ""
        if not url:
            url = careers_url

        # Prefer desktop meta location list (first ul before second-list).
        loc_block = re.search(
            r'class="hrt-card-meta-desktop"[^>]*>\s*<ul class="hrt-card-info-list">(.*?)</ul>',
            content,
            re.DOTALL | re.IGNORECASE,
        )
        location_parts: list[str] = []
        if loc_block:
            for span in re.findall(r"<span>([^<]+)</span>", loc_block.group(1)):
                label = unescape(re.sub(r"\s+", " ", span)).strip()
                if label and label not in location_parts:
                    location_parts.append(label)

        desc_m = re.search(
            r'class="hrt-card-description"[^>]*>(.*?)</p>',
            content,
            re.DOTALL | re.IGNORECASE,
        )
        requirements = ""
        if desc_m:
            requirements = unescape(re.sub(r"<[^>]+>", " ", desc_m.group(1)))
            requirements = re.sub(r"\s+", " ", requirements).strip()
        if not requirements:
            requirements = unescape(str(item.get("description") or "")).strip()

        jobs.append(
            normalize_job(
                job_id=f"hrt:{job_id}",
                title=title,
                company=company_name,
                url=url,
                location=", ".join(location_parts),
                requirements=requirements,
            )
        )

    print(f"[hrt] fetched {len(jobs)} jobs")
    return jobs
