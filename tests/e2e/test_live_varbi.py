from __future__ import annotations

import asyncio
import csv
import os

import pytest

from ats_scrapers.scrapers import VarbiScraper
from scripts.run_pipeline import CONFIGS, DATA_ROOT

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit real Varbi endpoints",
    ),
]


async def test_live_varbi_karolinska() -> None:
    config = CONFIGS["varbi"]
    with (DATA_ROOT / config["csv"]).open(newline="", encoding="utf-8") as handle:
        row = next(row for row in csv.DictReader(handle) if row["slug"] == "ki")
    async with asyncio.timeout(90):
        jobs = await VarbiScraper(row["slug"], **config["kwargs"](row)).afetch()
    assert jobs
    assert len({job.ats_id for job in jobs}) == len(jobs)
    assert all(job.title and job.description for job in jobs)
    assert all(job.company == "Karolinska Institutet (KI)" for job in jobs)
    assert all(str(job.url).startswith("https://ki.varbi.com/") for job in jobs)
    assert any(str(job.apply_url).startswith("https://ki.varbi.com/") for job in jobs if job.apply_url)
    assert any((job.raw or {}).get("application_deadline_date") for job in jobs)
    assert any(job.requisition_id for job in jobs)
    assert any(job.location and job.country_iso for job in jobs)
