"""Config, resume, seen_jobs, and candidates persistence."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
RESUME_PATH = ROOT / "resume.txt"
SEEN_PATH = ROOT / "seen_jobs.json"
CANDIDATES_PATH = ROOT / "candidates.json"


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


def filter_unseen(jobs: list[dict[str, str]], seen: dict[str, Any]) -> list[dict[str, str]]:
    return [job for job in jobs if job["id"] not in seen]


def clear_candidates(path: Path = CANDIDATES_PATH) -> None:
    """Wipe candidates.json at run start so stale results are never mixed with a new scrape."""
    if path.exists():
        path.unlink()
        print(f"Cleared {path.name}")


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

def write_candidates_summary(jobs: list[dict[str, str]], *, unseen_count: int) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## Deterministic filter candidates",
        "",
        f"_Survived deterministic filters: **{len(jobs)}** "
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
