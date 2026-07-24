#!/usr/bin/env python3
"""Daily job matcher entrypoint (thin wrapper around job_matcher.cli)."""

from __future__ import annotations

from job_matcher.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
