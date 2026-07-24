"""Source adapters: fetch jobs from configured career sites."""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import urlparse

from job_matcher.sources.amazon import fetch_amazon
from job_matcher.sources.apple import fetch_apple
from job_matcher.sources.ashby import fetch_ashby
from job_matcher.sources.bloomberg import fetch_bloomberg
from job_matcher.sources.bytedance import fetch_bytedance
from job_matcher.sources.citadel import fetch_citadel, fetch_citadel_securities
from job_matcher.sources.deshaw import fetch_deshaw
from job_matcher.sources.eightfold import fetch_eightfold
from job_matcher.sources.goldman import _GOLDMAN_GRAPHQL_URL, fetch_goldman
from job_matcher.sources.google import fetch_google
from job_matcher.sources.greenhouse import fetch_greenhouse
from job_matcher.sources.hrt import fetch_hrt
from job_matcher.sources.icims import fetch_icims_api
from job_matcher.sources.lever import fetch_lever
from job_matcher.sources.linkedin import fetch_linkedin
from job_matcher.sources.meta import fetch_meta
from job_matcher.sources.microsoft import fetch_microsoft
from job_matcher.sources.oracle_cloud import fetch_oracle_cloud
from job_matcher.sources.placeholder import fetch_placeholder
from job_matcher.sources.twosigma import fetch_twosigma
from job_matcher.sources.uber import fetch_uber
from job_matcher.sources.workable import fetch_workable
from job_matcher.sources.workday import fetch_workday


def _fetch_site(site: dict[str, Any]) -> list[dict[str, str]]:
    """Dispatch one config website entry to the matching adapter."""
    site_type = (site.get("type") or "").lower()
    company = site.get("name") or ""

    if site_type == "greenhouse":
        token = site.get("board_token")
        if not token:
            return []
        return fetch_greenhouse(
            token,
            company=company or token,
            metadata_includes=site.get("metadata_includes") or None,
        )
    if site_type == "ashby":
        board = site.get("board_name") or site.get("board_token")
        if not board:
            return []
        return fetch_ashby(board, company=company or board)
    if site_type == "lever":
        lever_site = site.get("site") or site.get("board_token")
        if not lever_site:
            return []
        return fetch_lever(lever_site, company=company or lever_site)
    if site_type == "workable":
        account = site.get("account") or site.get("board_token") or site.get("site")
        if not account:
            return []
        return fetch_workable(account, company=company or account)
    if site_type == "eightfold":
        domain = site.get("domain")
        host = site.get("host")
        if not host and site.get("url"):
            host = urlparse(site["url"]).netloc
        if not domain or not host:
            return []
        return fetch_eightfold(domain, host, company=company or domain)
    if site_type == "google":
        # Optional ATS company filter (e.g. "Google"); omit to include DeepMind etc.
        company_filter = site.get("company")
        if company_filter is None:
            company_filter = "Google"
        return fetch_google(
            company_filter=company_filter or None,
            display_name=company or "Google",
        )
    if site_type == "uber":
        return fetch_uber(company=company or "Uber")
    if site_type == "meta":
        return fetch_meta(company=company or "Meta")
    if site_type == "workday":
        host = site.get("myworkday_host") or site.get("host")
        wd_site = site.get("site") or site.get("board")
        if not host and site.get("url"):
            host = urlparse(site["url"]).netloc
        if not host or not wd_site:
            return []
        return fetch_workday(host, wd_site, company=company or wd_site)
    if site_type == "amazon":
        return fetch_amazon(company=company or "Amazon")
    if site_type == "apple":
        return fetch_apple(company=company or "Apple")
    if site_type == "microsoft":
        return fetch_microsoft(
            company=company or "Microsoft",
            domain=site.get("domain") or "microsoft.com",
            host=site.get("host") or "apply.careers.microsoft.com",
        )
    if site_type == "bytedance":
        return fetch_bytedance(
            company=company or "ByteDance",
            host=site.get("host") or "jobs.bytedance.com",
            portal_type=int(site.get("portal_type") or 2),
            keyword=site.get("keyword") or "",
        )
    if site_type == "icims_api":
        api_url = site.get("api_url")
        if not api_url:
            return []
        return fetch_icims_api(api_url, company=company or "iCIMS")
    if site_type == "bloomberg":
        return fetch_bloomberg(
            company=company or "Bloomberg",
            search_url=site.get("search_url")
            or site.get("url")
            or "https://bloomberg.avature.net/careers/SearchJobs/",
        )
    if site_type == "linkedin":
        company_ids = site.get("company_ids") or "1337"
        if isinstance(company_ids, list):
            company_ids = ",".join(str(x) for x in company_ids)
        return fetch_linkedin(
            company=company or "LinkedIn",
            company_ids=str(company_ids),
        )
    if site_type == "citadel_securities":
        return fetch_citadel_securities(
            company=company or "Citadel Securities",
            url=site.get("url")
            or "https://www.citadelsecurities.com/careers/open-opportunities/",
        )
    if site_type == "citadel":
        return fetch_citadel(
            company=company or "Citadel",
            url=site.get("url") or "https://www.citadel.com/careers/open-opportunities/",
        )
    if site_type == "twosigma":
        return fetch_twosigma(
            company=company or "Two Sigma",
            search_url=site.get("search_url")
            or site.get("url")
            or "https://careers.twosigma.com/careers/OpenRoles/",
        )
    if site_type == "deshaw":
        return fetch_deshaw(
            company=company or "D. E. Shaw",
            url=site.get("url") or "https://www.deshaw.com/careers",
        )
    if site_type == "hrt":
        return fetch_hrt(
            company=company or "Hudson River Trading",
            careers_url=site.get("url") or "https://www.hudsonrivertrading.com/careers/",
        )
    if site_type == "goldman":
        return fetch_goldman(
            company=company or "Goldman Sachs",
            graphql_url=site.get("graphql_url") or _GOLDMAN_GRAPHQL_URL,
            experiences=site.get("experiences") or None,
        )
    if site_type == "oracle_cloud":
        host = site.get("host")
        site_number = site.get("site_number") or site.get("siteNumber")
        if not host and site.get("url"):
            host = urlparse(site["url"]).netloc
        if not host or not site_number:
            return []
        return fetch_oracle_cloud(
            host,
            str(site_number),
            company=company or host,
        )
    if site_type in {"placeholder", "documentation"}:
        # Documentation / non-API targets are skipped at fetch time.
        return []

    print(f"[fetch] unknown site type {site_type!r} for {company or site}", file=sys.stderr)
    return []


def fetch_all(config: dict[str, Any]) -> list[dict[str, str]]:
    """Fetch and dedupe jobs from all configured sources (no title/location filters)."""
    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for site in config.get("websites") or []:
        company = site.get("name") or ""
        site_type = (site.get("type") or "").lower()
        try:
            fetched = _fetch_site(site)
        except Exception as exc:
            print(f"[fetch] {company or site_type} failed: {exc}", file=sys.stderr)
            continue

        for job in fetched:
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            jobs.append(job)

    return jobs
