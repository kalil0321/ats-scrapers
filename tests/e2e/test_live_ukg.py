"""Live smoke tests for UKG Pro Recruiting."""

from __future__ import annotations

import asyncio
import os

import pytest

from ats_scrapers.scrapers.ukg import UKGProScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real UKG endpoints",
    ),
]


async def test_live_ukg_smoke() -> None:
    async with asyncio.timeout(180):
        jobs = await UKGProScraper(
            "https://recruiting.ultipro.com/com1074clcl/JobBoard/"
            "8a0d300a-f96c-4397-8ab4-86c9c5e8ab57",
            company_name="Comprehensive Logistics",
        ).afetch()

    assert jobs
    assert len({job.ats_id for job in jobs}) == len(jobs)
    assert all(job.title.strip() for job in jobs)
    assert all(job.description for job in jobs)
    assert all(":" in job.global_id for job in jobs)
    assert all("ultipro.com" in str(job.url) for job in jobs)


async def test_live_ukg_large_catalog_paginates() -> None:
    async with asyncio.timeout(180):
        jobs = await UKGProScraper(
            "https://recruiting.ultipro.com/SUR1004SRGY/JobBoard/"
            "aa616d8f-f2a8-46c2-8f8e-1ca56e162ffd",
            company_name="Surgery Partners",
            include_descriptions=False,
        ).afetch()

    assert len(jobs) > 50
    assert len({job.ats_id for job in jobs}) == len(jobs)
