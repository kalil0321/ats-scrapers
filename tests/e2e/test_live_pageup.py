"""Live smoke test for PageUp."""

from __future__ import annotations

import asyncio
import os

import pytest

from ats_scrapers.scrapers.pageup import PageUpScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real PageUp endpoints",
    ),
]


async def test_live_pageup_smoke() -> None:
    async with asyncio.timeout(180):
        jobs = await PageUpScraper(
            "902/cw/en",
            company_name="Arts Centre Melbourne",
        ).afetch()

    assert jobs
    assert len({job.ats_id for job in jobs}) == len(jobs)
    assert all(job.title.strip() for job in jobs)
    assert all(job.description for job in jobs)
    assert all(job.ats_id.startswith("902/cw/en:") for job in jobs)
    assert all("careers.pageuppeople.com" in str(job.url) for job in jobs)


async def test_live_pageup_large_catalog_paginates() -> None:
    async with asyncio.timeout(180):
        jobs = await PageUpScraper(
            "873/cw/en-us",
            company_name="California State University",
            include_descriptions=False,
        ).afetch()

    assert len(jobs) > 1000
    assert len({job.ats_id for job in jobs}) == len(jobs)
