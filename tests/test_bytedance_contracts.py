"""Integration contracts for the ByteDance scraper."""

from __future__ import annotations

from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_bytedance_dedup_priority_matches_employer_sources() -> None:
    assert ATS_DEDUP_PRIORITY["bytedance"] == ATS_DEDUP_PRIORITY["workday"]


def test_bytedance_singleton_fails_closed_on_empty() -> None:
    assert CONFIGS["bytedance"]["fail_closed_on_empty"] is True
