"""Shared HTTP helpers for source adapters."""

from __future__ import annotations

import re
import sys
from html import unescape

import requests

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional until Meta/ByteDance/Citadel are fetched
    curl_requests = None

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|svg)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Cap so Gemini prompts stay within practical size when falling back to full pages.
MAX_PAGE_TEXT_CHARS = 80_000


def html_to_text(html: str, *, max_chars: int = MAX_PAGE_TEXT_CHARS) -> str:
    """Strip scripts/styles/tags from HTML into plain text."""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html or "")
    text = unescape(_TAG_RE.sub(" ", cleaned))
    text = _WS_RE.sub(" ", text).strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + "…"
    return text


def fetch_page_text(url: str, *, timeout: int = 45) -> str:
    """Fetch a job URL and return plain page text (curl_cffi first, then requests)."""
    url = (url or "").strip()
    if not url:
        return ""

    html = ""
    if curl_requests is not None:
        try:
            resp = curl_requests.get(url, impersonate="chrome", timeout=timeout)
            resp.raise_for_status()
            html = resp.text or ""
        except Exception as exc:  # noqa: BLE001 — fall through to requests
            print(f"[page] curl_cffi failed for {url}: {exc}", file=sys.stderr)

    if not html:
        try:
            resp = requests.get(
                url,
                headers={**BROWSER_HEADERS, "Accept": "text/html"},
                timeout=timeout,
            )
            resp.raise_for_status()
            html = resp.text or ""
        except requests.RequestException as exc:
            print(f"[page] fetch failed for {url}: {exc}", file=sys.stderr)
            return ""

    if "Just a moment" in html and "cf-" in html.lower():
        print(f"[page] Cloudflare challenge for {url}", file=sys.stderr)
        return ""

    return html_to_text(html)
