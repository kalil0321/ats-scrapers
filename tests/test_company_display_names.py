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


def test_phenom_hostname_gets_display_name() -> None:
    job = _job("jobs.bell.ca")
    _apply_display_name(job, "https://jobs.bell.ca", {"https://jobs.bell.ca": "Bell Canada"})
    assert job.company == "Bell Canada"


def test_oracle_hostname_gets_display_name() -> None:
    slug = "https://ebwg.fa.us2.oraclecloud.com?site_number=CX"
    job = _job("ebwg.fa.us2.oraclecloud.com")
    _apply_display_name(job, slug, {slug: "ACI Worldwide"})
    assert job.company == "ACI Worldwide"


def test_personio_subdomain_host_gets_display_name() -> None:
    job = _job("4screen.jobs.personio.com")
    _apply_display_name(job, "4screen", {"4screen": "4Screen GmbH"})
    assert job.company == "4Screen GmbH"


def test_other_domain_name_is_kept() -> None:
    # A source-provided name that only looks like a domain stays.
    job = _job("monday.com")
    _apply_display_name(job, "acme", {"acme": "Acme"})
    assert job.company == "monday.com"
