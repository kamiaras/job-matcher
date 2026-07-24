"""Shared HTTP helpers for source adapters."""

from __future__ import annotations

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
