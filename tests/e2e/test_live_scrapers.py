"""Live end-to-end scraper tests — real tenants, real HTTP.

Opt in with ``ATS_SCRAPERS_LIVE_E2E=1``; select with ``-m live``:

    ATS_SCRAPERS_LIVE_E2E=1 pytest -m live -q

Covers one representative tenant per major source. Big always-hiring
boards assert a non-empty result; small long-tail tenants only assert
that the fetch round-trips (their boards may be legitimately empty).
The nightly ``live-e2e`` workflow runs this suite so scraper drift
(an ATS changing its API) surfaces within a day, not when a user
reports it.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from ats_scrapers.models import Job
from ats_scrapers.scrapers import (
    AshbyScraper,
    BambooHRScraper,
    BreezyScraper,
    GreenhouseScraper,
    HerpScraper,
    LeverScraper,
    PinpointScraper,
    RecruiteeScraper,
    RemoteOKScraper,
    SmartRecruitersScraper,
    TeamtailorScraper,
    TheHubScraper,
    UberScraper,
    WeWorkRemotelyScraper,
    WorkableScraper,
    WorkdayScraper,
    YCombinatorScraper,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real ATS endpoints",
    ),
]

TIMEOUT = 150.0

# (id, factory, expect_jobs) — expect_jobs=True only for boards that
# are essentially never empty.
CASES = [
    ("greenhouse-anthropic", lambda: GreenhouseScraper("anthropic"), True),
    ("herp-herpinc", lambda: HerpScraper("herpinc", include_descriptions=False), True),
    ("lever-palantir", lambda: LeverScraper("palantir"), True),
    ("ashby-openai", lambda: AshbyScraper("openai", include_descriptions=False), True),
    ("smartrecruiters-10pearls",
     lambda: SmartRecruitersScraper("10pearls", include_descriptions=False), False),
    ("workable-0x", lambda: WorkableScraper("0x", include_descriptions=False), False),
    ("recruitee-12build",
     lambda: RecruiteeScraper("12build", include_descriptions=False), False),
    ("teamtailor-1komma5",
     lambda: TeamtailorScraper("1komma5", include_descriptions=False), False),
    ("breezy-10-4-truck-recruiting",
     lambda: BreezyScraper("10-4-truck-recruiting", include_descriptions=False), False),
    ("bamboohr-10web",
     lambda: BambooHRScraper("10web", include_descriptions=False), False),
    ("pinpoint-aawdc",
     lambda: PinpointScraper("aawdc", include_descriptions=False), False),
    ("workday-2020companies",
     lambda: WorkdayScraper.from_url(
         "https://2020companies.wd1.myworkdayjobs.com/external_careers",
         include_descriptions=False,
         max_fetch_seconds=90,
     ), True),
    ("remoteok", lambda: RemoteOKScraper("remoteok"), True),
    ("weworkremotely", lambda: WeWorkRemotelyScraper("weworkremotely"), True),
    ("thehub", lambda: TheHubScraper("thehub", include_descriptions=False), True),
    ("ycombinator",
     lambda: YCombinatorScraper("ycombinator", include_descriptions=False), True),
    ("uber", lambda: UberScraper("uber", include_descriptions=False), True),
]


def _assert_sane(job: Job) -> None:
    assert job.title, "job has no title"
    assert str(job.url).startswith("http"), "job has no absolute url"
    assert ":" in job.global_id, (
        f"global_id fell back to UUID (broken ats_id extraction): {job.global_id}"
    )
    if job.fetched_at is not None:
        assert job.fetched_at.tzinfo is not None, "fetched_at is naive, expected UTC"


@pytest.mark.parametrize(
    ("factory", "expect_jobs"),
    [pytest.param(f, e, id=i) for i, f, e in CASES],
)
async def test_live_fetch(factory, expect_jobs) -> None:
    scraper = factory()
    async with asyncio.timeout(TIMEOUT):
        jobs = await scraper.afetch()
    assert isinstance(jobs, list)
    if expect_jobs:
        assert jobs, f"{scraper!r} returned no jobs from a board that is never empty"
    for job in jobs[:25]:
        _assert_sane(job)


async def test_live_descriptions_greenhouse() -> None:
    """Greenhouse ships descriptions inline — verify they survive parsing."""
    async with asyncio.timeout(TIMEOUT):
        jobs = await GreenhouseScraper("anthropic").afetch()
    with_description = sum(1 for j in jobs[:50] if j.description)
    assert with_description > 0, "no descriptions parsed from content=true payload"


async def test_live_sync_wrapper_inside_running_loop() -> None:
    """The Jupyter/FastAPI story: sync fetch() from inside a live loop."""
    jobs = RemoteOKScraper("remoteok").fetch()
    assert isinstance(jobs, list) and jobs
