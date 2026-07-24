"""Eightfold public careers API."""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape

import requests

from job_matcher.normalize import normalize_job

# Headings that carry hard requirements (site-specific aliases included).
_REQUIREMENTS_HEADINGS = (
    "what you bring",
    "requirements",
    "qualifications",
    "basic qualifications",
    "minimum qualifications",
    "required qualifications",
    "who you are",
    "you bring",
    "must have",
    "must-have",
)

_HEADING_RE = re.compile(
    r"<p>\s*<b>\s*([^<]+?)\s*</b>\s*</p>"
    r"|<b>\s*([^<]+?)\s*</b>"
    r"|<h[1-4][^>]*>\s*([^<]+?)\s*</h[1-4]>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    text = unescape(_TAG_RE.sub(" ", html or ""))
    return _WS_RE.sub(" ", text).strip()


def _normalize_heading(raw: str) -> str:
    return _WS_RE.sub(" ", unescape(raw or "")).strip().lower().rstrip(":")


def _is_requirements_heading(heading: str) -> bool:
    h = _normalize_heading(heading)
    if not h:
        return False
    for alias in _REQUIREMENTS_HEADINGS:
        if h == alias or h.startswith(alias + " ") or alias in h:
            return True
    return False


def _extract_requirements(job_description_html: str) -> str:
    """Pull requirements-like sections from an Eightfold HTML job description.

    Millennium (career.mlp.com) labels this \"What You Bring\" instead of
    \"Requirements\". Falls back to the full description text when no known
    heading is present.
    """
    html = (job_description_html or "").strip()
    if not html:
        return ""

    matches = list(_HEADING_RE.finditer(html))
    if not matches:
        return _html_to_text(html)

    parts: list[str] = []
    for i, match in enumerate(matches):
        heading = next((g for g in match.groups() if g), "") or ""
        if not _is_requirements_heading(heading):
            continue
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        body = _html_to_text(html[start:end])
        if body:
            parts.append(f"{_WS_RE.sub(' ', unescape(heading)).strip()}:\n{body}")

    if parts:
        return "\n\n".join(parts)
    return _html_to_text(html)


def _location_from_item(item: dict) -> str:
    locs = item.get("locations") or []
    if isinstance(locs, list) and locs:
        return ", ".join(str(x) for x in locs if x)
    return (item.get("location") or "").strip()


def _fetch_job_detail(host: str, domain: str, job_id: str) -> str:
    """Fetch one position's HTML job_description from the Eightfold detail API."""
    url = f"https://{host}/api/apply/v2/jobs/{job_id}"
    resp = requests.get(url, params={"domain": domain}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        return ""
    return (payload.get("job_description") or "").strip()


def fetch_eightfold(
    domain: str,
    host: str,
    company: str | None = None,
    *,
    page_size: int = 10,
    detail_workers: int = 8,
) -> list[dict[str, str]]:
    """Fetch jobs from an Eightfold public careers API (e.g. Netflix, Millennium).

    The list endpoint returns empty descriptions; each position is enriched via
    the detail API so requirements (including \"What You Bring\") reach the LLM.
    """
    api_url = f"https://{host}/api/apply/v2/jobs"
    company_name = company or domain
    stubs: list[dict[str, str]] = []
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

            url = (item.get("canonicalPositionUrl") or "").strip()
            if not url:
                url = f"https://{host}/careers/job/{job_id}"

            list_desc = (item.get("job_description") or "").strip()
            stubs.append(
                {
                    "id": job_id,
                    "title": (item.get("name") or item.get("posting_name") or "").strip()
                    or "Untitled",
                    "url": url,
                    "location": _location_from_item(item),
                    "list_desc": list_desc,
                }
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

    jobs: list[dict[str, str]] = []
    failures = 0

    def _enrich(stub: dict[str, str]) -> dict[str, str]:
        desc_html = stub["list_desc"]
        if not desc_html:
            try:
                desc_html = _fetch_job_detail(host, domain, stub["id"])
            except requests.RequestException as exc:
                print(
                    f"[eightfold:{domain}] detail failed for {stub['id']}: {exc}",
                    file=sys.stderr,
                )
                raise
        return normalize_job(
            job_id=f"eightfold:{domain}:{stub['id']}",
            title=stub["title"],
            company=company_name,
            url=stub["url"],
            location=stub["location"],
            requirements=_extract_requirements(desc_html),
        )

    need_detail = [s for s in stubs if not s["list_desc"]]
    have_desc = [s for s in stubs if s["list_desc"]]

    for stub in have_desc:
        jobs.append(_enrich(stub))

    if need_detail:
        workers = max(1, min(detail_workers, len(need_detail)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_enrich, stub): stub for stub in need_detail}
            for future in as_completed(futures):
                stub = futures[future]
                try:
                    jobs.append(future.result())
                except Exception:  # noqa: BLE001 — keep remaining jobs
                    failures += 1
                    jobs.append(
                        normalize_job(
                            job_id=f"eightfold:{domain}:{stub['id']}",
                            title=stub["title"],
                            company=company_name,
                            url=stub["url"],
                            location=stub["location"],
                            requirements="",
                        )
                    )

    print(
        f"[eightfold:{domain}] fetched {len(jobs)} jobs"
        + (f" ({failures} detail failures)" if failures else "")
    )
    return jobs
