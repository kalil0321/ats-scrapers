"""Live smoke tests for the anonymous Dayforce job-feed API."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import pytest

from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import DayforceScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real job-board endpoints",
    ),
]


async def test_live_dayforce_public_feed() -> None:
    async with asyncio.timeout(60):
        jobs = await DayforceScraper(
            "mayfair/CANDIDATEPORTAL",
            company_name="Mayfair Diagnostics",
        ).afetch()

    assert jobs
    assert len({job.ats_id for job in jobs}) == len(jobs)
    for job in jobs:
        assert job.ats_type is ATSType.DAYFORCE
        assert job.title.strip()
        assert job.company == "Mayfair Diagnostics"
        assert job.description
        assert job.requisition_id
        assert (urlparse(str(job.url)).hostname or "") == "jobs.dayforcehcm.com"
