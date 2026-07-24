"""Gemini LLM job-requirements matcher."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

from job_matcher.sources._http import fetch_page_text

# Free-tier Gemini Flash is typically ~5 RPM per model; pace requests accordingly.
# Two Flash-Lite models share the work so each model's quota can be used.
DEFAULT_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
DEFAULT_REQUEST_INTERVAL_SEC = 7.0
DEFAULT_MAX_RETRIES = 8
DEFAULT_MAX_JOBS_PER_RUN = 40

_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_match.txt"
SYSTEM_PROMPT = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


def ensure_requirements(job: dict[str, str]) -> str:
    """Return non-empty requirements text for the LLM.

    If the scraper left requirements blank, fetch the job URL and use the whole
    page text instead. Mutates job['requirements'] when a page fallback succeeds.
    """
    existing = (job.get("requirements") or "").strip()
    if existing:
        return existing

    url = (job.get("url") or "").strip()
    page_text = fetch_page_text(url) if url else ""
    if page_text:
        print(
            f"[match] empty requirements for {job.get('id')}; "
            f"using full page text ({len(page_text)} chars)",
            file=sys.stderr,
        )
        job["requirements"] = page_text
        return page_text

    print(
        f"[match] empty requirements and page fetch failed for {job.get('id')} ({url})",
        file=sys.stderr,
    )
    return ""


class MatchResult(BaseModel):
    is_match: bool
    missing_requirements: list[str] = Field(default_factory=list)
    reason: str


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


def parse_models() -> list[str]:
    """Resolve Gemini model list from GEMINI_MODELS or GEMINI_MODEL."""
    raw = os.environ.get("GEMINI_MODELS") or os.environ.get("GEMINI_MODEL") or ""
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or list(DEFAULT_MODELS)


def match_job(
    job: dict[str, str],
    resume_lines: list[str],
    client: genai.Client,
    *,
    models: list[str],
    max_retries: int = DEFAULT_MAX_RETRIES,
    model_start_index: int = 0,
) -> MatchResult:
    if not models:
        raise ValueError("models must not be empty")

    requirements = ensure_requirements(job)
    user_prompt = (
        f"Company: {job.get('company', '')}\n"
        f"URL: {job.get('url', '')}\n\n"
        f"Requirements section:\n{requirements or '(empty — page fetch failed)'}\n\n"
        f"Resume lines:\n" + "\n".join(f"- {line}" for line in resume_lines)
    )

    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        model = models[(model_start_index + attempt - 1) % len(models)]
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
                next_model = models[(model_start_index + attempt) % len(models)]
                # Quotas are per-model: finish a full model cycle before sleeping.
                if len(models) > 1 and (attempt % len(models)) != 0:
                    print(
                        f"[match] 429 on {model} for {job.get('id')}; "
                        f"trying {next_model} ({attempt}/{attempts})",
                        file=sys.stderr,
                    )
                    continue
                delay = retry_sleep_seconds(exc, attempt)
                print(
                    f"[match] 429 on {model} for {job.get('id')}; "
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
