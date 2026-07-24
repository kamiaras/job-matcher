"""Goldman Sachs Higher GraphQL role search."""

from __future__ import annotations

import re
import sys
import uuid
from html import unescape
from typing import Any

import requests

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import BROWSER_HEADERS

_GOLDMAN_GRAPHQL_URL = "https://api-higher.gs.com/gateway/api/v1/graphql"
_GOLDMAN_ROLE_SEARCH_QUERY = """
query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    page { pageNumber pageSize hasNext }
    items {
      roleId
      jobTitle
      corporateTitle
      division
      jobFunction
      shortDescription
      locations { city state country primary }
    }
  }
}
""".strip()


def _goldman_format_location(locs: Any) -> str:
    """Join Goldman Higher location objects into a display string."""
    if not isinstance(locs, list) or not locs:
        return ""
    parts: list[str] = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        bits = [loc.get("city") or "", loc.get("state") or "", loc.get("country") or ""]
        label = ", ".join(b for b in bits if b)
        if label and label not in parts:
            parts.append(label)
    return " | ".join(parts)


def fetch_goldman(
    company: str | None = None,
    *,
    graphql_url: str = _GOLDMAN_GRAPHQL_URL,
    page_size: int = 50,
    experiences: list[str] | None = None,
) -> list[dict[str, str]]:
    """Fetch roles from Goldman Sachs Higher GraphQL (api-higher.gs.com)."""
    company_name = company or "Goldman Sachs"
    exp = experiences or ["PROFESSIONAL", "EARLY_CAREER"]
    jobs: list[dict[str, str]] = []
    page_number = 1
    total: int | None = None

    while True:
        headers = {
            **_BROWSER_HEADERS,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://higher.gs.com",
            "Referer": "https://higher.gs.com/results",
            "x-higher-request-id": str(uuid.uuid4()),
            "x-higher-session-id": str(uuid.uuid4()),
        }
        variables = {
            "searchQueryInput": {
                "page": {"pageNumber": page_number, "pageSize": page_size},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"},
                "experiences": exp,
            }
        }
        try:
            resp = requests.post(
                graphql_url,
                headers=headers,
                json={
                    "operationName": "GetRoles",
                    "query": _GOLDMAN_ROLE_SEARCH_QUERY,
                    "variables": variables,
                },
                timeout=45,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[goldman] fetch failed at page={page_number}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[goldman] invalid JSON at page={page_number}: {exc}", file=sys.stderr)
            break

        if payload.get("errors"):
            print(f"[goldman] GraphQL errors at page={page_number}: {payload['errors']}", file=sys.stderr)
            break

        data = (payload.get("data") or {}).get("roleSearch") or {}
        if total is None:
            try:
                total = int(data.get("totalCount")) if data.get("totalCount") is not None else None
            except (TypeError, ValueError):
                total = None

        items = data.get("items") or []
        if not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue
            role_id = str(item.get("roleId") or "").strip()
            title = (item.get("jobTitle") or "").strip()
            if not role_id or not title:
                continue
            desc = unescape(str(item.get("shortDescription") or ""))
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            jobs.append(
                normalize_job(
                    job_id=f"goldman:{role_id}",
                    title=title,
                    company=company_name,
                    url=f"https://higher.gs.com/roles/{role_id}",
                    location=_goldman_format_location(item.get("locations")),
                    requirements=desc,
                )
            )

        page_meta = data.get("page") or {}
        has_next = bool(page_meta.get("hasNext"))
        if not has_next:
            break
        if total is not None and len(jobs) >= total:
            break
        page_number += 1
        if page_number > 500:
            break

    print(f"[goldman] fetched {len(jobs)} jobs")
    return jobs
