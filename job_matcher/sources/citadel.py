"""Citadel and Citadel Securities careers HTML listings."""

from __future__ import annotations

import re
import sys
from html import unescape

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS, curl_requests

_CITADEL_CARD_RE = re.compile(
    r'<a\s+([^>]*class="[^"]*careers-listing-card[^"]*"[^>]*)>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_citadel_listing_cards(html: str, *, id_prefix: str, company_name: str) -> list[dict[str, str]]:
    """Parse Citadel / Citadel Securities evergreen careers-listing-card HTML."""
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _CITADEL_CARD_RE.finditer(html):
        attrs, body = match.group(1), match.group(2)
        href_m = re.search(r'href="([^"]+)"', attrs)
        if not href_m:
            continue
        href = href_m.group(1).strip()

        title = ""
        for pat in (
            r'data-position="([^"]+)"',
            r'aria-label="([^"]+)"',
            r'<h2[^>]*aria-label="([^"]+)"',
            r"<h2[^>]*>([^<]+)",
        ):
            title_m = re.search(pat, attrs + body, re.IGNORECASE)
            if title_m:
                title = title_m.group(1)
                break
        if not title:
            continue

        title = unescape(title.replace("&#8211;", "–").replace("&amp;", "&"))
        title = re.sub(r"^\s*Apply to\s+", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue

        loc_m = re.search(
            r'careers-listing-card__location[^>]*>\s*([^<]+)',
            body,
            re.IGNORECASE,
        )
        location = re.sub(r"\s+", " ", loc_m.group(1)).strip() if loc_m else ""

        slug = href.rstrip("/").rsplit("/", 1)[-1] or title
        if slug in seen:
            continue
        seen.add(slug)

        if href.startswith("/"):
            # Prefer host from an absolute sibling URL in the same page when possible.
            host_m = re.search(r'https://www\.(citadel(?:securities)?\.com)/', html)
            host = host_m.group(0).rstrip("/") if host_m else "https://www.citadel.com"
            href = f"{host}{href}"

        jobs.append(
            normalize_job(
                job_id=f"{id_prefix}:{slug}",
                title=title,
                company=company_name,
                url=href,
                location=location,
            )
        )
    return jobs


def _fetch_citadel_html(
    *,
    url: str,
    company_name: str,
    id_prefix: str,
    log_name: str,
) -> list[dict[str, str]]:
    """Fetch Citadel-family careers HTML (Cloudflare-prone; prefer curl_cffi)."""
    html = ""
    if curl_requests is not None:
        try:
            resp = curl_requests.get(url, impersonate="chrome", timeout=60)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            print(f"[{log_name}] curl_cffi fetch failed: {exc}", file=sys.stderr)
    if not html:
        try:
            resp = requests.get(
                url, headers={**_BROWSER_HEADERS, "Accept": "text/html"}, timeout=45
            )
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException as exc:
            print(f"[{log_name}] fetch failed: {exc}", file=sys.stderr)
            return []

    if "Just a moment" in html and "cf-" in html.lower():
        print(
            f"[{log_name}] blocked by Cloudflare; install/use curl-cffi",
            file=sys.stderr,
        )
        return []

    jobs = _parse_citadel_listing_cards(html, id_prefix=id_prefix, company_name=company_name)
    print(f"[{log_name}] fetched {len(jobs)} jobs")
    return jobs


def fetch_citadel_securities(
    company: str | None = None,
    *,
    url: str = "https://www.citadelsecurities.com/careers/open-opportunities/",
) -> list[dict[str, str]]:
    """Fetch Citadel Securities open roles from the careers HTML listing."""
    return _fetch_citadel_html(
        url=url,
        company_name=company or "Citadel Securities",
        id_prefix="citadel_securities",
        log_name="citadel_securities",
    )


def fetch_citadel(
    company: str | None = None,
    *,
    url: str = "https://www.citadel.com/careers/open-opportunities/",
) -> list[dict[str, str]]:
    """Fetch Citadel (hedge fund) open roles from the careers HTML listing."""
    return _fetch_citadel_html(
        url=url,
        company_name=company or "Citadel",
        id_prefix="citadel",
        log_name="citadel",
    )
