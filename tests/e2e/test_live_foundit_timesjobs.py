"""Live smoke tests for Foundit and TimesJobs."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from urllib.parse import urlparse

import pytest

from ats_scrapers.models import Job
from ats_scrapers.scrapers import FounditScraper, TimesJobsScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real job-board endpoints",
    ),
]

CASES: list[tuple[str, Callable[[], object], str]] = [
    ("foundit-in", lambda: FounditScraper("in", max_pages=1), "foundit.in"),
    (
        "timesjobs",
        lambda: TimesJobsScraper("timesjobs", max_pages=1),
        "timesjobs.com",
    ),
]


@pytest.mark.parametrize(
    ("factory", "expected_domain"),
    [pytest.param(factory, domain, id=name) for name, factory, domain in CASES],
)
async def test_live_jobboard_smoke(
    factory: Callable[[], object], expected_domain: str
) -> None:
    async with asyncio.timeout(180):
        jobs = await factory().afetch()
    assert jobs
    sample: list[Job] = jobs[:50]
    assert len({job.global_id for job in sample}) == len(sample)
    for job in sample:
        assert job.title.strip()
        assert job.company.strip()
        assert job.ats_id
        host = (urlparse(str(job.url)).hostname or "").lower()
        assert host == expected_domain or host.endswith(f".{expected_domain}")
        assert not host.endswith((".local", ".internal"))
