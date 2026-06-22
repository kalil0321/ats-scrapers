from __future__ import annotations

import re
from datetime import datetime

from jobhive.models import ATSType, Job
from jobhive.scrapers import BreezyScraper, SmartRecruitersScraper, WorkableScraper
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

TWO_STEP_PROVIDERS = {
    ATSType.AVATURE,
    ATSType.BAMBOOHR,
    ATSType.BREEZY,
    ATSType.BUNDESAGENTUR,
    ATSType.EIGHTFOLD,
    ATSType.EURES,
    ATSType.GEM,
    ATSType.GOOGLE,
    ATSType.ICIMS,
    ATSType.JAZZHR,
    ATSType.JOBSCH,
    ATSType.JOIN_COM,
    ATSType.MANFRED,
    ATSType.META,
    ATSType.ORACLE,
    ATSType.PERSONIO,
    ATSType.PROGRAMATHOR,
    ATSType.RIPPLING,
    ATSType.SMARTRECRUITERS,
    ATSType.TALEO,
    ATSType.TESLA,
    ATSType.WANTED,
    ATSType.WELLFOUND,
    ATSType.WORKABLE,
    ATSType.WORKDAY,
}


def _job(
    *,
    ats_type: ATSType,
    ats_id: str = "job-1",
    url: str = "https://example.com/jobs/job-1",
    description: str | None = None,
) -> Job:
    return Job(
        url=url,
        title="Engineer",
        company="Acme",
        ats_type=ats_type,
        ats_id=ats_id,
        description=description,
        fetched_at=datetime.now(),
    )


def test_two_step_providers_override_get_description() -> None:
    missing = [
        ats.value
        for ats in sorted(TWO_STEP_PROVIDERS, key=lambda item: item.value)
        if ScraperRegistry.get(ats).get_description is BaseScraper.get_description
    ]

    assert missing == []


def test_base_get_description_returns_existing_description() -> None:
    class DummyScraper(BaseScraper):
        ats = ATSType.CUSTOM

        def fetch(self) -> list[Job]:
            return []

    job = _job(ats_type=ATSType.CUSTOM, description="Already known.")

    assert DummyScraper("dummy").get_description(job) == "Already known."


def test_base_enrich_descriptions_uses_get_description() -> None:
    class DummyScraper(BaseScraper):
        ats = ATSType.CUSTOM

        def fetch(self) -> list[Job]:
            return []

        def get_description(self, job: Job) -> str | None:
            return f"Description for {job.ats_id}"

    job = _job(ats_type=ATSType.CUSTOM, description=None)

    DummyScraper("dummy").enrich_descriptions([job])

    assert job.description == "Description for job-1"


def test_workable_get_description_fetches_markdown(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://apply.workable.com/acme/jobs/view/ABC123.md",
        text="# Engineer\n\nBuild useful products.",
    )
    job = _job(
        ats_type=ATSType.WORKABLE,
        ats_id="ABC123",
        url="https://apply.workable.com/acme/j/ABC123",
    )

    description = WorkableScraper("acme").get_description(job)

    assert description == "# Engineer\n\nBuild useful products."
    assert job.description is None


def test_smartrecruiters_get_description_fetches_detail_api(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.smartrecruiters.com/v1/companies/acme/postings/123",
        json={
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": "<p>Build APIs.</p>"},
                    "qualifications": {"text": "<p>Write tests.</p>"},
                }
            }
        },
    )
    job = _job(
        ats_type=ATSType.SMARTRECRUITERS,
        ats_id="123",
        url="https://jobs.smartrecruiters.com/acme/123",
    )

    description = SmartRecruitersScraper("acme").get_description(job)

    assert description == "Build APIs.\n\nWrite tests."
    assert job.description is None


def test_breezy_get_description_fetches_detail_html(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://acme\.breezy\.hr/p/123"),
        text="<html><body><div class='description'>Build hiring tools.</div></body></html>",
    )
    job = _job(
        ats_type=ATSType.BREEZY,
        ats_id="123",
        url="https://acme.breezy.hr/p/123-engineer",
    )

    description = BreezyScraper("acme").get_description(job)

    assert description == "Build hiring tools."
    assert job.description is None
