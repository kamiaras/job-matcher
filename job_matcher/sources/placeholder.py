"""Placeholder sample jobs for pipeline smoke tests."""

from __future__ import annotations

from typing import Any

from job_matcher.normalize import normalize_job

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
