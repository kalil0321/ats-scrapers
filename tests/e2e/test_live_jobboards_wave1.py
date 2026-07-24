"""Live smoke tests for the credential-free job-board wave."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from urllib.parse import urlparse

import pytest

from ats_scrapers.models import Job
from ats_scrapers.scrapers import (
    BumeranScraper,
    BytedanceScraper,
    ElempleoScraper,
    FounditScraper,
    InfoJobsBrasilScraper,
    JobBankCAScraper,
    JobThaiScraper,
    MyCareersFutureScraper,
    SeekScraper,
    TimesJobsScraper,
    TorreScraper,
    VietnamWorksScraper,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real job-board endpoints",
    ),
]

TIMEOUT = 180.0

CASES: list[tuple[str, Callable[[], object], str]] = [
    ("bumeran-ar", lambda: BumeranScraper("ar", max_pages=1), "bumeran.com.ar"),
    ("bytedance", lambda: BytedanceScraper("bytedance"), "joinbytedance.com"),
    ("elempleo", lambda: ElempleoScraper("elempleo", max_pages=1), "elempleo.com"),
    (
        "foundit-in",
        lambda: FounditScraper("in", max_pages=1),
        "foundit.in",
    ),
    (
        "infojobs-br",
        lambda: InfoJobsBrasilScraper("infojobs_br", max_pages=1),
        "infojobs.com.br",
    ),
    (
        "jobbank-ca",
        lambda: JobBankCAScraper("jobbankca", max_pages=1),
        "jobbank.gc.ca",
    ),
    (
        "jobthai",
        lambda: JobThaiScraper("jobthai", max_pages_per_type=1),
        "jobthai.com",
    ),
    (
        "mycareersfuture",
        lambda: MyCareersFutureScraper("mycareersfuture"),
        "mycareersfuture.gov.sg",
    ),
    ("seek-au", lambda: SeekScraper("au", max_pages=1), "seek.com"),
    (
        "timesjobs",
        lambda: TimesJobsScraper("timesjobs", max_pages=1),
        "timesjobs.com",
    ),
    ("torre", lambda: TorreScraper("torre", max_pages=1), "torre"),
    (
        "vietnamworks",
        lambda: VietnamWorksScraper("vietnamworks", max_pages=1),
        "vietnamworks.com",
    ),
]


def _assert_real_jobs(jobs: list[Job], expected_domain: str) -> None:
    assert jobs, "live source returned no jobs"
    sample = jobs[:50]
    assert len({job.global_id for job in sample}) == len(sample)
    for job in sample:
        assert job.title.strip()
        assert job.company.strip()
        assert job.ats_id
        host = (urlparse(str(job.url)).hostname or "").lower()
        assert expected_domain in host
        assert not host.endswith((".local", ".internal"))
        assert job.fetched_at is None or job.fetched_at.tzinfo is not None


@pytest.mark.parametrize(
    ("factory", "expected_domain"),
    [pytest.param(factory, domain, id=name) for name, factory, domain in CASES],
)
async def test_live_jobboard_smoke(
    factory: Callable[[], object],
    expected_domain: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MYCAREERSFUTURE_MAX_PAGES", "1")
    scraper = factory()
    async with asyncio.timeout(TIMEOUT):
        jobs = await scraper.afetch()
    _assert_real_jobs(jobs, expected_domain)
