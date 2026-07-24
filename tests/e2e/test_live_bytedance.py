"""Live smoke test for ByteDance careers."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from urllib.parse import urlparse

import pytest

from ats_scrapers.models import Job
from ats_scrapers.scrapers import BytedanceScraper
from ats_scrapers.scrapers.base import BaseScraper

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real job-board endpoints",
    ),
]

CASES: list[tuple[str, Callable[[], BaseScraper], str]] = [
    ("bytedance", lambda: BytedanceScraper("bytedance"), "joinbytedance.com"),
]


@pytest.mark.parametrize(
    ("factory", "expected_domain"),
    [pytest.param(factory, domain, id=name) for name, factory, domain in CASES],
)
async def test_live_jobboard_smoke(
    factory: Callable[[], BaseScraper], expected_domain: str
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
        assert expected_domain in host
        assert not host.endswith((".local", ".internal"))
