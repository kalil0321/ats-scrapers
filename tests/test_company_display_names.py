"""Pipeline: tenant display names replace slug-valued companies."""

from __future__ import annotations

from ats_scrapers.models import ATSType, Job
from scripts.run_pipeline import _apply_display_name


def _job(company: str) -> Job:
    return Job(
        url="https://jobs.lever.co/acme/1",
        title="Engineer",
        company=company,
        ats_type=ATSType.LEVER,
        ats_id="1",
    )


def test_slug_company_gets_display_name() -> None:
    job = _job("acme-corp")
    _apply_display_name(job, "acme-corp", {"acme-corp": "Acme Corp"})
    assert job.company == "Acme Corp"


def test_real_company_name_is_kept() -> None:
    job = _job("Acme Holdings")
    _apply_display_name(job, "acme-corp", {"acme-corp": "Acme Corp"})
    assert job.company == "Acme Holdings"


def test_missing_display_name_keeps_slug() -> None:
    job = _job("acme")
    _apply_display_name(job, "acme", {})
    assert job.company == "acme"
