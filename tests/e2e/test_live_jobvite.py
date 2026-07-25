"""Live smoke test for Jobvite."""

from __future__ import annotations

import asyncio
import os

import pytest

from ats_scrapers.scrapers.jobvite import JobviteScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real Jobvite endpoints",
    ),
]


async def test_live_jobvite_smoke() -> None:
    async with asyncio.timeout(180):
        jobs = await JobviteScraper(
            "sitecore",
            company_name="Sitecore",
        ).afetch()

    assert jobs
    assert len({job.ats_id for job in jobs}) == len(jobs)
    assert all(job.title.strip() for job in jobs)
    assert all(job.description for job in jobs)
    assert all("jobs.jobvite.com" in str(job.url) for job in jobs)
