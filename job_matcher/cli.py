"""CLI entry: fetch → deterministic filters → optional LLM match."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

from dotenv import load_dotenv
from google import genai

from job_matcher import filters
from job_matcher.matcher import (
    DEFAULT_MAX_JOBS_PER_RUN,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_INTERVAL_SEC,
    MatchResult,
    is_rate_limit_error,
    match_job,
    parse_models,
    _env_float,
    _env_int,
)
from job_matcher.persistence import (
    CANDIDATES_PATH,
    ROOT,
    SEEN_PATH,
    clear_candidates,
    filter_unseen,
    load_config,
    load_resume,
    load_seen,
    save_seen,
    utc_now_iso,
    write_candidates,
    write_candidates_summary,
    write_github_summary,
)
from job_matcher.sources import fetch_all, fetch_placeholder

load_dotenv(ROOT / ".env")


def fetch_jobs(config: dict[str, Any]) -> list[dict[str, str]]:
    """Fetch from sources, apply deterministic filters, fall back to placeholders."""
    jobs = fetch_all(config)
    jobs = filters.apply(jobs, config)

    if not jobs:
        print("[fetch] no live jobs returned; using placeholder samples")
        seen_ids: set[str] = set()
        for job in fetch_placeholder(config):
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                jobs.append(job)

    return jobs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch jobs, apply deterministic filters, then score with Gemini."
    )
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help=(
            "Stop after title/location filters; write candidates.json and skip Gemini "
            "and seen_jobs.json updates."
        ),
    )
    return parser.parse_args(argv)


def run_deterministic_only(config: dict[str, Any], seen: dict[str, Any]) -> int:
    clear_candidates()
    candidates = fetch_jobs(config)
    unseen = filter_unseen(candidates, seen)

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
    write_candidates_summary(candidates, unseen_count=len(unseen))
    print(f"\nWrote {len(candidates)} candidates to {CANDIDATES_PATH.name}")
    return 0


def run_llm_match(config: dict[str, Any], seen: dict[str, Any]) -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is required (set in .env or environment)", file=sys.stderr)
        return 1

    models = parse_models()
    request_interval = _env_float("GEMINI_REQUEST_INTERVAL_SEC", DEFAULT_REQUEST_INTERVAL_SEC)
    max_retries = _env_int("GEMINI_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    max_jobs = _env_int("GEMINI_MAX_JOBS_PER_RUN", DEFAULT_MAX_JOBS_PER_RUN)
    resume_lines = load_resume()
    if not resume_lines:
        print("ERROR: resume.txt has no usable experience lines", file=sys.stderr)
        return 1

    all_jobs = fetch_jobs(config)
    unseen = filter_unseen(all_jobs, seen)
    print(f"[models] {', '.join(models)}")
    if max_jobs > 0 and len(unseen) > max_jobs:
        print(
            f"Fetched {len(all_jobs)} jobs; {len(unseen)} unseen; "
            f"scoring {max_jobs} this run (GEMINI_MAX_JOBS_PER_RUN)"
        )
        unseen = unseen[:max_jobs]
    else:
        print(f"Fetched {len(all_jobs)} jobs; {len(unseen)} unseen")

    client = genai.Client(api_key=api_key)
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
                models=models,
                max_retries=max_retries,
                model_start_index=index % len(models),
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    seen = load_seen()
    if args.deterministic_only:
        return run_deterministic_only(config, seen)
    return run_llm_match(config, seen)
