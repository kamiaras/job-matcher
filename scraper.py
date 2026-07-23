#!/usr/bin/env python3
"""Daily job matcher: fetch → dedupe → strict LLM match → persist seen_jobs.json."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
RESUME_PATH = ROOT / "resume.txt"
SEEN_PATH = ROOT / "seen_jobs.json"

# Free-tier Gemini Flash is typically ~5 RPM; pace requests accordingly.
DEFAULT_REQUEST_INTERVAL_SEC = 13.0
DEFAULT_MAX_RETRIES = 8
DEFAULT_MAX_JOBS_PER_RUN = 40

load_dotenv(ROOT / ".env")

SYSTEM_PROMPT = """You are a strict job-requirements matcher. Nothing more, nothing less.

Rules:
1. Compare ONLY the job's Requirements section against the candidate's resume lines.
2. Also verify the job location is in the US (including remote-US / nationwide US). Reject non-US locations.
3. Explicitly IGNORE salary, benefits, culture, perks, company description, and any "nice to have" framing that is outside Requirements.
4. Do NOT soft-match preferences or infer skills that are not clearly supported by a resume line.
5. A job is a match only if every hard requirement in the Requirements section is supported by the resume AND the location is US-eligible.
6. List any unmet hard requirements in missing_requirements. If is_match is true, missing_requirements should be empty.
7. Keep reason short (one or two sentences).
"""


class MatchResult(BaseModel):
    is_match: bool
    missing_requirements: list[str] = Field(default_factory=list)
    reason: str


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_resume(path: Path = RESUME_PATH) -> list[str]:
    lines: list[str] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


def load_seen(path: Path = SEEN_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def save_seen(seen: dict[str, Any], path: Path = SEEN_PATH) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)
        f.write("\n")


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


def filter_unseen(jobs: list[dict[str, str]], seen: dict[str, Any]) -> list[dict[str, str]]:
    return [job for job in jobs if job["id"] not in seen]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError) and exc.code == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "too many requests" in text


def _parse_retry_delay_seconds(exc: BaseException) -> float | None:
    """Extract server-suggested wait from Gemini RetryInfo / error message."""
    details = getattr(exc, "details", None)
    candidates: list[Any] = []
    if isinstance(details, dict):
        err = details.get("error") if isinstance(details.get("error"), dict) else details
        if isinstance(err, dict):
            nested = err.get("details")
            if isinstance(nested, list):
                candidates.extend(nested)
            msg = err.get("message")
            if isinstance(msg, str):
                candidates.append({"message": msg})
    candidates.append({"message": str(exc)})

    for item in candidates:
        if isinstance(item, dict):
            delay = item.get("retryDelay")
            if isinstance(delay, str):
                m = re.fullmatch(r"(\d+(?:\.\d+)?)s?", delay.strip())
                if m:
                    return float(m.group(1))
            if isinstance(delay, (int, float)):
                return float(delay)
            msg = item.get("message")
            if isinstance(msg, str):
                m = re.search(r"retry in\s+(\d+(?:\.\d+)?)\s*s", msg, re.I)
                if m:
                    return float(m.group(1))
    return None


def retry_sleep_seconds(exc: BaseException, attempt: int) -> float:
    """Prefer API retryDelay; otherwise exponential backoff with a sane ceiling."""
    suggested = _parse_retry_delay_seconds(exc)
    if suggested is not None:
        return min(max(suggested, 1.0) + 1.0, 120.0)
    return min(2**attempt, 60.0)


def match_job(
    job: dict[str, str],
    resume_lines: list[str],
    client: genai.Client,
    *,
    model: str,
    location_requirement: str = "US",
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> MatchResult:
    user_prompt = (
        f"Location requirement: {location_requirement}\n\n"
        f"Job title: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Listed location: {job.get('location', '')}\n"
        f"URL: {job.get('url', '')}\n\n"
        f"Requirements section:\n{job.get('requirements') or '(empty)'}\n\n"
        f"Resume lines:\n" + "\n".join(f"- {line}" for line in resume_lines)
    )

    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=f"{SYSTEM_PROMPT}\n\n{user_prompt}",
                config={
                    "response_mime_type": "application/json",
                    "response_schema": MatchResult,
                },
            )
            break
        except Exception as exc:  # noqa: BLE001 — classify rate limits vs hard failures
            if is_rate_limit_error(exc) and attempt < attempts:
                delay = retry_sleep_seconds(exc, attempt)
                print(
                    f"[match] 429 for {job.get('id')}; "
                    f"retry {attempt}/{attempts} in {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
    else:
        raise RuntimeError(f"match_job exhausted retries for {job.get('id')}")

    raw = (response.text or "").strip()
    if not raw:
        return MatchResult(
            is_match=False,
            missing_requirements=["LLM returned no structured output"],
            reason="Failed to parse structured match result.",
        )
    return MatchResult.model_validate_json(raw)


def write_github_summary(matches: list[dict[str, Any]]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## Job matches",
        "",
    ]
    if not matches:
        lines.append("_No matches today._")
    else:
        lines.extend(
            [
                "| Title | Company | Location | URL | Reason |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for m in matches:
            title = (m.get("title") or "").replace("|", "\\|")
            company = (m.get("company") or "").replace("|", "\\|")
            location = (m.get("location") or "").replace("|", "\\|")
            url = m.get("url") or ""
            reason = (m.get("reason") or "").replace("|", "\\|").replace("\n", " ")
            link = f"[link]({url})" if url else ""
            lines.append(f"| {title} | {company} | {location} | {link} | {reason} |")

    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is required (set in .env or environment)", file=sys.stderr)
        return 1

    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    request_interval = _env_float("GEMINI_REQUEST_INTERVAL_SEC", DEFAULT_REQUEST_INTERVAL_SEC)
    max_retries = _env_int("GEMINI_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    max_jobs = _env_int("GEMINI_MAX_JOBS_PER_RUN", DEFAULT_MAX_JOBS_PER_RUN)
    config = load_config()
    resume_lines = load_resume()
    if not resume_lines:
        print("ERROR: resume.txt has no usable experience lines", file=sys.stderr)
        return 1

    seen = load_seen()
    all_jobs = fetch_jobs(config)
    unseen = filter_unseen(all_jobs, seen)
    if max_jobs > 0 and len(unseen) > max_jobs:
        print(
            f"Fetched {len(all_jobs)} jobs; {len(unseen)} unseen; "
            f"scoring {max_jobs} this run (GEMINI_MAX_JOBS_PER_RUN)"
        )
        unseen = unseen[:max_jobs]
    else:
        print(f"Fetched {len(all_jobs)} jobs; {len(unseen)} unseen")

    client = genai.Client(api_key=api_key)
    location_requirement = config.get("location_requirement") or "US"
    matches: list[dict[str, Any]] = []
    now = utc_now_iso()
    stopped_for_rate_limit = False

    for index, job in enumerate(unseen):
        if index > 0 and request_interval > 0:
            time.sleep(request_interval)

        try:
            result = match_job(
                job,
                resume_lines,
                client,
                model=model,
                location_requirement=location_requirement,
                max_retries=max_retries,
            )
        except Exception as exc:  # noqa: BLE001 — keep run going on single-job failures
            print(f"[match] failed for {job.get('id')}: {exc}", file=sys.stderr)
            if is_rate_limit_error(exc):
                # Leave this job (and the rest) unseen so the next run can retry.
                stopped_for_rate_limit = True
                print(
                    "[match] rate limited after retries; "
                    "leaving remaining jobs unseen for the next run",
                    file=sys.stderr,
                )
                break
            result = MatchResult(
                is_match=False,
                missing_requirements=["matcher_error"],
                reason=f"Matcher error: {exc}",
            )

        # Record every LLM-evaluated job (match or not) so it is never re-scored.
        seen[job["id"]] = {
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "url": job.get("url", ""),
            "matched": bool(result.is_match),
            "seen_at": now,
        }
        # Persist immediately so a mid-run crash/timeout does not re-send these.
        save_seen(seen)

        status = "MATCH" if result.is_match else "skip"
        print(f"[{status}] {job.get('company')} — {job.get('title')}: {result.reason}")

        if result.is_match:
            matches.append(
                {
                    "title": job.get("title", ""),
                    "company": job.get("company", ""),
                    "location": job.get("location", ""),
                    "url": job.get("url", ""),
                    "reason": result.reason,
                    "missing_requirements": result.missing_requirements,
                }
            )

    write_github_summary(matches)
    if stopped_for_rate_limit:
        print(
            f"Stopped early due to rate limits. {len(matches)} match(es). "
            f"Updated {SEEN_PATH.name}."
        )
        return 0
    print(f"Done. {len(matches)} match(es). Updated {SEEN_PATH.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
