from __future__ import annotations

import asyncio

import pytest

from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._posting_detail import fill_missing


def _jobs(count: int) -> list[Job]:
    return [
        Job(
            url=f"https://example.com/jobs/{number}", title="Welder", company="Acme",
            ats_type=ATSType.HIRINGTHING, ats_id=str(number),
        )
        for number in range(count)
    ]


def test_fills_only_the_jobs_without_a_description() -> None:
    jobs = _jobs(5)
    jobs[1].description = "Already here."
    read: list[str] = []

    async def enrich(job: Job) -> None:
        read.append(job.ats_id)
        job.description = "Read."

    asyncio.run(fill_missing(jobs, enrich, 2))

    assert sorted(read) == ["0", "2", "3", "4"]
    assert jobs[1].description == "Already here."


def test_one_failure_stops_every_worker() -> None:
    jobs = _jobs(40)
    started: list[str] = []

    async def enrich(job: Job) -> None:
        started.append(job.ats_id)
        if job.ats_id == "0":
            raise RuntimeError("page gone")
        await asyncio.sleep(0.01)

    async def run() -> int:
        with pytest.raises(RuntimeError, match="page gone"):
            await fill_missing(jobs, enrich, 4)
        seen = len(started)
        # Workers left running would start more jobs during this pause.
        await asyncio.sleep(0.1)
        return len(started) - seen

    assert asyncio.run(run()) == 0
    assert len(started) < len(jobs)
