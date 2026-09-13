from __future__ import annotations

import asyncio
import os

import pytest

from ats_scrapers.scrapers import VarbiScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real Varbi endpoints",
    ),
]


async def test_live_varbi_karolinska() -> None:
    async with asyncio.timeout(90):
        jobs = await VarbiScraper("ki").afetch()
    assert jobs
    assert len({job.ats_id for job in jobs}) == len(jobs)
    assert all(job.title and job.description and job.location for job in jobs)
    assert all(job.company == "Karolinska Institutet (KI)" for job in jobs)
    assert all(str(job.url).startswith("https://ki.varbi.com/") for job in jobs)
