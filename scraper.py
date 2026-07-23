#!/usr/bin/env python3
"""Deterministic job pre-filter: fetch → title match → title exclusion → dump candidates.

No LLM calls. Use this branch to inspect what survives config.json filters and
iterate on more deterministic rules before scoring with Gemini.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen_jobs.json"
CANDIDATES_PATH = ROOT / "candidates.json"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_seen(path: Path = SEEN_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def normalize_job(
    *,
    job_id: str,
    title: str,
    company: str,
    url: str,
    location: str = "",
    requirements: str = "",
) -> dict[str, str]:
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "url": url,
        "location": location or "",
        "requirements": requirements or "",
    }


def fetch_greenhouse(board_token: str, company: str | None = None) -> list[dict[str, str]]:
    """Fetch jobs from Greenhouse public boards API."""
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    try:
        resp = requests.get(api_url, params={"content": "true"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[greenhouse:{board_token}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    jobs_raw = payload.get("jobs") or []
    company_name = company or board_token
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        job_id = str(item.get("id") or item.get("absolute_url") or "")
        if not job_id:
            continue
        location = ""
        loc = item.get("location")
        if isinstance(loc, dict):
            location = loc.get("name") or ""
        elif isinstance(loc, str):
            location = loc

        # Greenhouse sometimes embeds HTML content; keep as requirements text.
        requirements = (item.get("content") or item.get("description") or "").strip()
        jobs.append(
            normalize_job(
                job_id=f"greenhouse:{board_token}:{job_id}",
                title=(item.get("title") or "").strip() or "Untitled",
                company=company_name,
                url=(item.get("absolute_url") or "").strip(),
                location=location,
                requirements=requirements,
            )
        )
    print(f"[greenhouse:{board_token}] fetched {len(jobs)} jobs")
    return jobs


def fetch_ashby(board_name: str, company: str | None = None) -> list[dict[str, str]]:
    """Fetch jobs from Ashby public job posting API."""
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"
    try:
        resp = requests.get(api_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[ashby:{board_name}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    jobs_raw = payload.get("jobs") or []
    company_name = company or board_name
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        if item.get("isListed") is False:
            continue
        job_id = str(item.get("id") or item.get("jobUrl") or "")
        if not job_id:
            continue

        location = (item.get("location") or "").strip()
        secondary = item.get("secondaryLocations") or []
        if isinstance(secondary, list) and secondary:
            extras = [
                (s.get("location") if isinstance(s, dict) else str(s) or "").strip()
                for s in secondary
            ]
            extras = [e for e in extras if e]
            if extras:
                location = ", ".join([location, *extras] if location else extras)

        if item.get("isRemote") or (item.get("workplaceType") or "").lower() == "remote":
            if location and "remote" not in location.lower():
                location = f"{location} (Remote)"
            elif not location:
                location = "Remote"

        requirements = (item.get("descriptionPlain") or item.get("descriptionHtml") or "").strip()
        jobs.append(
            normalize_job(
                job_id=f"ashby:{board_name}:{job_id}",
                title=(item.get("title") or "").strip() or "Untitled",
                company=company_name,
                url=(item.get("jobUrl") or item.get("applyUrl") or "").strip(),
                location=location,
                requirements=requirements,
            )
        )
    print(f"[ashby:{board_name}] fetched {len(jobs)} jobs")
    return jobs


def _lever_requirements(item: dict[str, Any]) -> str:
    """Combine Lever plain description + section lists into one requirements blob."""
    parts: list[str] = []
    plain = (item.get("descriptionPlain") or item.get("description") or "").strip()
    if plain:
        parts.append(plain)

    for section in item.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = (section.get("text") or "").strip()
        content = (section.get("content") or "").strip()
        if heading and content:
            parts.append(f"{heading}\n{content}")
        elif content:
            parts.append(content)

    additional = (item.get("additionalPlain") or item.get("additional") or "").strip()
    if additional:
        parts.append(additional)
    return "\n\n".join(parts).strip()


def fetch_lever(site: str, company: str | None = None) -> list[dict[str, str]]:
    """Fetch jobs from Lever public postings API."""
    api_url = f"https://api.lever.co/v0/postings/{site}"
    try:
        resp = requests.get(api_url, params={"mode": "json"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[lever:{site}] fetch failed: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    jobs_raw = payload if isinstance(payload, list) else (payload.get("data") or [])
    company_name = company or site
    jobs: list[dict[str, str]] = []

    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or item.get("hostedUrl") or "")
        if not job_id:
            continue

        categories = item.get("categories") or {}
        location = ""
        if isinstance(categories, dict):
            location = (categories.get("location") or "").strip()
            all_locs = categories.get("allLocations") or []
            if isinstance(all_locs, list) and all_locs:
                joined = ", ".join(str(x) for x in all_locs if x)
                if joined:
                    location = joined

        workplace = (item.get("workplaceType") or "").strip()
        if workplace and workplace.lower() == "remote":
            if location and "remote" not in location.lower():
                location = f"{location} (Remote)"
            elif not location:
                location = "Remote"

        jobs.append(
            normalize_job(
                job_id=f"lever:{site}:{job_id}",
                title=(item.get("text") or "").strip() or "Untitled",
                company=company_name,
                url=(item.get("hostedUrl") or item.get("applyUrl") or "").strip(),
                location=location,
                requirements=_lever_requirements(item),
            )
        )
    print(f"[lever:{site}] fetched {len(jobs)} jobs")
    return jobs


def fetch_placeholder(config: dict[str, Any]) -> list[dict[str, str]]:
    """Sample jobs so the pipeline can be smoke-tested when live sources return nothing."""
    titles = config.get("titles") or ["Research Scientist"]
    sample_title = titles[0]
    return [
        normalize_job(
            job_id="placeholder:sample-us-match",
            title=sample_title,
            company="Example Labs",
            url="https://example.com/jobs/sample-us-match",
            location="Remote, United States",
            requirements=(
                "Requirements:\n"
                "- Ph.D. in Electrical and Computer Engineering or related field\n"
                "- Experience with machine learning and mathematical optimization\n"
                "- Strong Python skills (Pandas, NumPy, Scikit-Learn)\n"
                "- Research experience in sequential decision-making or bandits\n"
            ),
        ),
        normalize_job(
            job_id="placeholder:sample-eu-reject",
            title="ML Engineer",
            company="Example EU GmbH",
            url="https://example.com/jobs/sample-eu-reject",
            location="Berlin, Germany",
            requirements=(
                "Requirements:\n"
                "- 5+ years production ML experience\n"
                "- Ph.D. preferred\n"
                "- Must be based in Germany\n"
            ),
        ),
        normalize_job(
            job_id="placeholder:sample-missing-skills",
            title="Quantitative Researcher",
            company="Example Quant",
            url="https://example.com/jobs/sample-missing-skills",
            location="New York, NY, United States",
            requirements=(
                "Requirements:\n"
                "- 3+ years of C++ low-latency trading systems experience\n"
                "- Deep expertise in FPGA hardware acceleration\n"
                "- Prior market-making desk experience required\n"
            ),
        ),
    ]


def _normalize_title_text(text: str) -> str:
    """Lowercase and expand common ML/AI abbreviations for substring matching."""
    t = (text or "").lower()
    replacements = (
        ("machine learning", " ml "),
        ("artificial intelligence", " ai "),
        ("ops research", " operations research "),
        ("post-doc", " postdoc "),
        ("post doctoral", " postdoc "),
        ("phd", " phd "),
        ("ph.d.", " phd "),
        ("ph.d", " phd "),
    )
    for old, new in replacements:
        t = t.replace(old, new)
    return " ".join(t.split())


def title_matches(job_title: str, titles: list[str]) -> bool:
    """True if job title contains any configured title phrase (fuzzy on ML/AI)."""
    if not titles:
        return True
    normalized_job = _normalize_title_text(job_title)
    for want in titles:
        needle = _normalize_title_text(want)
        if needle and needle in normalized_job:
            return True
    return False


def title_excluded(job_title: str, exclusions: list[str]) -> bool:
    """True if job title contains any seniority / experience-level exclusion phrase.

    Uses word-boundary matching so short tokens like 'IV', 'Sr', or 'VP' do not
    false-positive inside unrelated words (e.g. 'private', 'SRE').
    """
    if not exclusions:
        return False
    normalized_job = _normalize_title_text(job_title)
    for phrase in exclusions:
        needle = _normalize_title_text(phrase)
        if not needle:
            continue
        # Escape so literals like 'Sr.' match safely; require token boundaries.
        pattern = r"(?:^|[^a-z0-9])" + re.escape(needle) + r"(?:[^a-z0-9]|$)"
        if re.search(pattern, normalized_job):
            return True
    return False


def fetch_jobs(config: dict[str, Any]) -> list[dict[str, str]]:
    """Orchestrate all configured sources; fall back to placeholders if empty."""
    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    titles = [t for t in (config.get("titles") or []) if isinstance(t, str) and t.strip()]
    exclusions = [
        t for t in (config.get("title_exclusions") or []) if isinstance(t, str) and t.strip()
    ]

    for site in config.get("websites") or []:
        site_type = (site.get("type") or "").lower()
        company = site.get("name") or ""

        if site_type == "greenhouse":
            token = site.get("board_token")
            if not token:
                continue
            fetched = fetch_greenhouse(token, company=company or token)
        elif site_type == "ashby":
            board = site.get("board_name") or site.get("board_token")
            if not board:
                continue
            fetched = fetch_ashby(board, company=company or board)
        elif site_type == "lever":
            lever_site = site.get("site") or site.get("board_token")
            if not lever_site:
                continue
            fetched = fetch_lever(lever_site, company=company or lever_site)
        elif site_type in {"placeholder", "documentation"}:
            # Documentation / non-API targets are skipped at fetch time.
            continue
        else:
            print(f"[fetch] unknown site type {site_type!r} for {company or site}", file=sys.stderr)
            continue

        for job in fetched:
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            jobs.append(job)

    if titles:
        before = len(jobs)
        jobs = [job for job in jobs if title_matches(job.get("title", ""), titles)]
        print(f"[filter] title match kept {len(jobs)}/{before} jobs")

    if exclusions:
        before = len(jobs)
        jobs = [job for job in jobs if not title_excluded(job.get("title", ""), exclusions)]
        print(f"[filter] title exclusion kept {len(jobs)}/{before} jobs")

    if not jobs:
        print("[fetch] no live jobs returned; using placeholder samples")
        for job in fetch_placeholder(config):
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                jobs.append(job)

    return jobs


def write_candidates(jobs: list[dict[str, str]], path: Path = CANDIDATES_PATH) -> None:
    """Write filtered candidates for inspection (title/company/location/url only)."""
    slim = [
        {
            "id": job.get("id", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
        }
        for job in jobs
    ]
    with path.open("w", encoding="utf-8") as f:
        json.dump(slim, f, indent=2, ensure_ascii=False)
        f.write("\n")


def write_github_summary(jobs: list[dict[str, str]], *, unseen_count: int) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## Deterministic filter candidates",
        "",
        f"_Survived title filters: **{len(jobs)}** "
        f"({unseen_count} not yet in `seen_jobs.json`). No LLM scoring._",
        "",
    ]
    if not jobs:
        lines.append("_No candidates after deterministic filters._")
    else:
        lines.extend(
            [
                "| Title | Company | Location | URL |",
                "| --- | --- | --- | --- |",
            ]
        )
        for job in jobs:
            title = (job.get("title") or "").replace("|", "\\|")
            company = (job.get("company") or "").replace("|", "\\|")
            location = (job.get("location") or "").replace("|", "\\|")
            url = job.get("url") or ""
            link = f"[link]({url})" if url else ""
            lines.append(f"| {title} | {company} | {location} | {link} |")

    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    config = load_config()
    seen = load_seen()
    candidates = fetch_jobs(config)
    unseen = [job for job in candidates if job["id"] not in seen]

    print(f"Fetched+filtered {len(candidates)} candidates; {len(unseen)} not in seen_jobs.json")
    print("(deterministic-only: no LLM, seen_jobs.json not updated)\n")

    for job in candidates:
        already = "seen" if job["id"] in seen else "new "
        print(
            f"[{already}] {job.get('company')} — {job.get('title')} "
            f"({job.get('location') or 'n/a'})"
        )
        if job.get("url"):
            print(f"         {job['url']}")

    write_candidates(candidates)
    write_github_summary(candidates, unseen_count=len(unseen))
    print(f"\nWrote {len(candidates)} candidates to {CANDIDATES_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
