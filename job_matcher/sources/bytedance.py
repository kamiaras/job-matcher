"""ByteDance / TikTok careers search API."""

from __future__ import annotations

import sys

from job_matcher.normalize import normalize_job
from job_matcher.sources._http import curl_requests

def fetch_bytedance(
    company: str | None = None,
    *,
    host: str = "jobs.bytedance.com",
    portal_type: int = 2,
    keyword: str = "",
    page_size: int = 50,
) -> list[dict[str, str]]:
    """Fetch jobs from ByteDance / TikTok careers search API.

    Requires ``curl_cffi`` (Chrome TLS). ``portal_type`` is accepted by the API
    but often ignored; use ``keyword`` (e.g. ``\"TikTok\"``) to narrow brands.
    """
    if curl_requests is None:
        print(
            "[bytedance] curl_cffi is required (pip install curl-cffi)",
            file=sys.stderr,
        )
        return []

    company_name = company or ("TikTok" if "tiktok" in keyword.lower() else "ByteDance")
    host = host.replace("https://", "").replace("http://", "").strip("/")
    origin = f"https://{host}"
    list_path = "/en/position" if "bytedance" in host else "/position"
    jobs: list[dict[str, str]] = []
    offset = 0
    total: int | None = None
    label = f"{host}:kw={keyword or '*'}"

    session = curl_requests.Session(impersonate="chrome")
    try:
        session.get(f"{origin}{list_path}", timeout=45)
    except Exception as exc:
        print(f"[bytedance:{label}] session warmup failed: {exc}", file=sys.stderr)

    while True:
        body = {
            "job_category_id_list": [],
            "keyword": keyword or "",
            "limit": page_size,
            "location_code_list": [],
            "offset": offset,
            "portal_entrance": 1,
            "portal_type": portal_type,
            "recruitment_id_list": [],
            "subject_id_list": [],
        }
        try:
            resp = session.post(
                f"{origin}/api/v1/search/job/posts",
                json=body,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "Referer": f"{origin}{list_path}",
                },
                timeout=45,
            )
            resp.raise_for_status()
        except Exception as exc:
            print(f"[bytedance:{label}] fetch failed at offset={offset}: {exc}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError as exc:
            print(f"[bytedance:{label}] invalid JSON at offset={offset}: {exc}", file=sys.stderr)
            break

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            print(f"[bytedance:{label}] unexpected payload at offset={offset}", file=sys.stderr)
            break

        if total is None:
            raw_count = data.get("count")
            try:
                total = int(raw_count) if raw_count is not None else None
            except (TypeError, ValueError):
                total = None

        posts = data.get("job_post_list") or []
        if not posts:
            break

        for item in posts:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id") or item.get("code") or "").strip()
            if not job_id:
                continue
            city = item.get("city_info") or {}
            location = ""
            if isinstance(city, dict):
                location = (city.get("en_name") or city.get("name") or "").strip()
            requirements = "\n\n".join(
                part
                for part in (
                    (item.get("description") or "").strip(),
                    (item.get("requirement") or "").strip(),
                )
                if part
            )
            jobs.append(
                normalize_job(
                    job_id=f"bytedance:{job_id}",
                    title=(item.get("title") or "").strip() or "Untitled",
                    company=company_name,
                    url=f"{origin}/en/position/{job_id}"
                    if "bytedance" in host
                    else f"{origin}/position/{job_id}",
                    location=location,
                    requirements=requirements,
                )
            )

        offset += len(posts)
        if total is not None and offset >= total:
            break
        if len(posts) < page_size:
            break
        if offset > 15_000:
            print(f"[bytedance:{label}] abort: pagination exceeded 15000", file=sys.stderr)
            break

    print(f"[bytedance:{label}] fetched {len(jobs)} jobs")
    return jobs
