"""Tests for the Pydantic models that define the public schema.

Field renames here are breaking changes — these tests pin the contract.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from jobhive.models import ATSType, Company, Job, Salary

# --- ATSType -----------------------------------------------------------------

def test_ats_type_includes_every_supported_platform() -> None:
    expected = {
        # Multi-tenant ATS systems
        "ashby", "avature", "cornerstone", "eightfold", "gem", "greenhouse",
        "icims", "join_com", "lever", "mercor", "oracle", "personio", "phenom",
        "pinpoint", "recruiterbox", "rippling", "smartrecruiters",
        "successfactors", "workable", "workday",
        # Big-tech custom careers systems
        "amazon", "apple", "google", "meta",
        "tesla", "tiktok", "uber", "usajobs",
        # National / supranational public-sector aggregators
        "bundesagentur", "arbetsformedlingen", "eures",
        # Hybrid jobboards (companies post directly)
        "welcometothejungle",
        # Additional multi-tenant ATSes
        "bamboohr", "breezy", "jazzhr", "jobvite",
        "recruitee", "taleo", "teamtailor",
        # Catch-all
        "custom",
    }
    assert {a.value for a in ATSType} == expected


def test_ats_type_is_string_enum() -> None:
    assert ATSType.GREENHOUSE == "greenhouse"
    assert str(ATSType.GREENHOUSE) == "greenhouse"


def test_ats_type_can_be_constructed_from_string() -> None:
    assert ATSType("lever") is ATSType.LEVER


# --- Salary ------------------------------------------------------------------

def test_salary_minimal() -> None:
    s = Salary(currency="USD")
    assert s.currency == "USD"
    assert s.period == "YEAR"
    assert s.min_amount is None


def test_salary_full() -> None:
    s = Salary(currency="EUR", period="MONTH", min_amount=4000, max_amount=6000, summary="4-6k")
    assert s.period == "MONTH"
    assert s.summary == "4-6k"


def test_salary_currency_must_be_three_chars() -> None:
    for bad in ["DOLLAR", "$$", "U", ""]:
        with pytest.raises(ValidationError):
            Salary(currency=bad)


def test_salary_period_must_be_one_of_known_values() -> None:
    with pytest.raises(ValidationError):
        Salary(currency="USD", period="FORTNIGHT")  # type: ignore[arg-type]


def test_salary_is_frozen() -> None:
    s = Salary(currency="USD")
    with pytest.raises(ValidationError):
        s.currency = "EUR"  # type: ignore[misc]


# --- Company -----------------------------------------------------------------

def test_company_minimal() -> None:
    c = Company(slug="openai", name="OpenAI", ats=ATSType.GREENHOUSE)
    assert c.slug == "openai"
    assert c.careers_url is None


def test_company_with_urls() -> None:
    c = Company(
        slug="openai",
        name="OpenAI",
        ats=ATSType.GREENHOUSE,
        careers_url="https://openai.com/careers",
        website="https://openai.com",
    )
    assert str(c.careers_url).startswith("https://openai.com/careers")


def test_company_rejects_invalid_url() -> None:
    with pytest.raises(ValidationError):
        Company(slug="x", name="X", ats=ATSType.LEVER, careers_url="ftp://nope")


def test_company_is_frozen() -> None:
    c = Company(slug="x", name="X", ats=ATSType.LEVER)
    with pytest.raises(ValidationError):
        c.name = "Y"  # type: ignore[misc]


# --- Job ---------------------------------------------------------------------

def _minimal_job(**overrides) -> Job:
    base = {
        "url": "https://example.com/job/1",
        "title": "Engineer",
        "company": "acme",
        "ats_type": ATSType.GREENHOUSE,
        "ats_id": "123",
    }
    base.update(overrides)
    return Job(**base)


def test_job_minimal_construction() -> None:
    job = _minimal_job()
    assert job.title == "Engineer"
    assert job.ats_type is ATSType.GREENHOUSE
    assert job.salary is None


def test_job_with_salary_returns_salary_object() -> None:
    job = _minimal_job(
        ats_type=ATSType.ASHBY,
        salary_currency="USD",
        salary_min=100_000,
        salary_max=180_000,
    )
    salary = job.salary
    assert isinstance(salary, Salary)
    assert salary.currency == "USD"
    assert salary.period == "YEAR"
    assert salary.min_amount == 100_000


def test_job_salary_period_propagates_to_salary_object() -> None:
    job = _minimal_job(salary_currency="USD", salary_period="HOUR", salary_min=50)
    assert job.salary is not None
    assert job.salary.period == "HOUR"


def test_job_rejects_invalid_url() -> None:
    with pytest.raises(ValidationError):
        _minimal_job(url="not-a-url")


def test_job_posted_at_accepts_datetime() -> None:
    when = datetime(2026, 1, 15, 12, 0, 0)
    job = _minimal_job(posted_at=when)
    assert job.posted_at == when


def test_job_posted_at_accepts_iso_string() -> None:
    job = _minimal_job(posted_at="2026-01-15T12:00:00")
    assert job.posted_at == datetime(2026, 1, 15, 12, 0, 0)


def test_job_accepts_ats_type_via_alias() -> None:
    job = Job.model_validate(
        {
            "url": "https://example.com/job/1",
            "title": "Engineer",
            "company": "acme",
            "ats_type": "lever",
            "ats_id": "abc",
        }
    )
    assert job.ats_type is ATSType.LEVER


def test_job_round_trips_through_model_dump() -> None:
    original = _minimal_job(salary_currency="USD", salary_min=100_000)
    payload = original.model_dump(mode="json")
    restored = Job.model_validate(payload)
    assert restored.salary_min == 100_000


def test_job_lat_lon_optional() -> None:
    job = _minimal_job(lat=37.7749, lon=-122.4194)
    assert job.lat == pytest.approx(37.7749)
