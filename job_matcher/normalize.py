"""Shared job record shape used by all sources."""

from __future__ import annotations


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
