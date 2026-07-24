"""Deterministic title and location filters."""

from __future__ import annotations

import re
from typing import Any

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


# Explicit US markers and common US city/state tokens for multi-location strings.
_US_POSITIVE_PHRASES = (
    "united states",
    "usa",
    "u.s.a",
    "u.s.",
    "remote - us",
    "remote-us",
    "remote, us",
    "remote, usa",
    "remote us",
    "nationwide",
    "united states of america",
)

# Two-letter US state / DC codes. Matched as whole tokens only.
_US_STATE_CODES = frozenset(
    {
        "al", "ak", "az", "ar", "ca", "co", "ct", "dc", "de", "fl", "ga", "hi",
        "ia", "id", "il", "in", "ks", "ky", "la", "ma", "md", "me", "mi", "mn",
        "mo", "ms", "mt", "nc", "nd", "ne", "nh", "nj", "nm", "nv", "ny", "oh",
        "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "va", "vt", "wa",
        "wi", "wv", "wy",
    }
)

_US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
)

_US_CITY_PHRASES = (
    "san francisco", "new york city", "new york", "nyc", "seattle", "chicago",
    "boston", "austin", "seattle", "denver", "dallas", "atlanta", "miami",
    "los angeles", "san diego", "san jose", "palo alto", "mountain view",
    "menlo park", "redwood city", "foster city", "san mateo", "sunnyvale",
    "cupertino", "santa clara", "kirkland", "bellevue", "redmond", "seattle",
    "washington dc", "washington d.c", "washington, d.c", "st. louis",
    "st louis", "pittsburgh", "philadelphia", "phoenix", "portland", "honolulu",
    "fayetteville", "hayward", "stamford", "detroit",
)


def _token_boundary_search(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    pattern = r"(?:^|[^a-z0-9])" + re.escape(needle) + r"(?:[^a-z0-9]|$)"
    return re.search(pattern, haystack) is not None


def _location_has_us_signal(location: str) -> bool:
    """True if location text clearly includes a US-eligible option."""
    text = " ".join((location or "").lower().replace("–", "-").replace("—", "-").split())
    if not text:
        return False

    # Bare "US" / "U.S." as a token (avoid matching inside other words).
    if _token_boundary_search(text, "us") or _token_boundary_search(text, "u.s"):
        return True

    for phrase in _US_POSITIVE_PHRASES:
        if phrase in text:
            return True

    for phrase in _US_STATE_NAMES:
        if _token_boundary_search(text, phrase):
            return True

    for phrase in _US_CITY_PHRASES:
        if phrase in text:
            return True

    # State codes: prefer ", CA" / " CA," / trailing " CA" style to reduce false hits.
    for code in _US_STATE_CODES:
        if re.search(rf"(?:^|[^a-z0-9]){code}(?:[^a-z0-9]|$)", text):
            # Require comma/space context typical of "City, CA" listings when short.
            if re.search(rf",\s*{code}(?:[^a-z0-9]|$)", text) or re.search(
                rf"(?:^|[^a-z0-9]){code}\s*,", text
            ):
                return True

    return False


def location_is_us_eligible(location: str, non_us_locations: list[str]) -> bool:
    """Keep US / ambiguous locations; drop clearly non-US-only postings.

    Multi-location strings that include any US option (e.g. "London | NYC") are kept.
    Empty / bare "Remote" is kept (ambiguous) so later filters can decide.
    """
    text = " ".join((location or "").lower().replace("–", "-").replace("—", "-").split())
    if not text:
        return True

    if _location_has_us_signal(text):
        return True

    for phrase in non_us_locations:
        needle = " ".join(phrase.lower().split())
        if needle and _token_boundary_search(text, needle):
            return False

    return True


def apply(jobs: list[dict[str, str]], config: dict[str, Any]) -> list[dict[str, str]]:
    """Apply title include/exclude and US-location filters from config."""
    titles = [t for t in (config.get("titles") or []) if isinstance(t, str) and t.strip()]
    exclusions = [
        t for t in (config.get("title_exclusions") or []) if isinstance(t, str) and t.strip()
    ]
    non_us_locations = [
        t for t in (config.get("non_us_locations") or []) if isinstance(t, str) and t.strip()
    ]

    if titles:
        before = len(jobs)
        jobs = [job for job in jobs if title_matches(job.get("title", ""), titles)]
        print(f"[filter] title match kept {len(jobs)}/{before} jobs")

    if exclusions:
        before = len(jobs)
        jobs = [job for job in jobs if not title_excluded(job.get("title", ""), exclusions)]
        print(f"[filter] title exclusion kept {len(jobs)}/{before} jobs")

    before = len(jobs)
    jobs = [
        job
        for job in jobs
        if location_is_us_eligible(job.get("location", ""), non_us_locations)
    ]
    print(f"[filter] US location kept {len(jobs)}/{before} jobs")
    return jobs
