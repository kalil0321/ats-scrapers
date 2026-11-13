"""Live smoke test for Paylocity."""

from __future__ import annotations

import asyncio
import os

import pytest

from ats_scrapers.scrapers.paylocity import PaylocityScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real Paylocity endpoints",
    ),
]


async def test_live_paylocity_smoke() -> None:
    async with asyncio.timeout(90):
        jobs = await PaylocityScraper(
            "273074a3-6ae3-4be1-b8da-31fcc05c9700",
            company_name="Little Sunshine's Playhouse",
        ).afetch()

    assert jobs
    assert len({job.ats_id for job in jobs}) == len(jobs)
    assert all(job.title.strip() for job in jobs)
    assert all(job.description for job in jobs)
    assert all(
        "recruiting.paylocity.com/Recruiting/Jobs/Details/" in str(job.url)
        for job in jobs
    )


async def test_live_paylocity_legacy_detail_template() -> None:
    async with asyncio.timeout(90):
        jobs = await PaylocityScraper(
            "b39f30b7-6c8b-4c9a-8dd6-2c1fbb7b4d49",
            company_name="Coopersburg Kenworth",
        ).afetch()

    assert jobs
    assert all(job.description for job in jobs)
