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
        "welcometothejungle", "getonbrd", "wanted", "remoteok",
        "weworkremotely", "programathor", "builtin", "jobsch",
        "manfred", "thehub", "themuse", "ycombinator", "wellfound",
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


# --- Job.global_id -----------------------------------------------------------
#
# The global_id is the cross-ATS unique identifier for a posting.
# Format: f"{ats_type}:{ats_id}". Falls back to a UUID4 when ats_id is
# missing or contains characters that would corrupt CSV / JSON output.


def test_global_id_default_format() -> None:
    job = _minimal_job(ats_type=ATSType.ASHBY, ats_id="abc-123")
    assert job.global_id == "ashby:abc-123"


def test_global_id_preserves_case() -> None:
    """Workday and a few other ATSes use mixed-case requisition IDs.
    Lowercasing them would collapse legitimately distinct postings."""
    job = _minimal_job(ats_type=ATSType.WORKDAY, ats_id="R0136150")
    assert job.global_id == "workday:R0136150"


def test_global_id_strips_whitespace_from_ats_id() -> None:
    """Apple's careers API occasionally trails an ats_id with a single
    space. Strip it both from ats_id and from the derived global_id."""
    job = _minimal_job(ats_type=ATSType.APPLE, ats_id="  200544316  ")
    assert job.ats_id == "200544316"
    assert job.global_id == "apple:200544316"


def test_global_id_keeps_internal_colons() -> None:
    """Some Taleo URLs encode multiple colons inside ats_id. The
    contract documents 'split on FIRST colon' so consumers can
    recover ats_type + ats_id."""
    job = _minimal_job(ats_type=ATSType.TALEO, ats_id="acme:req:12345")
    assert job.global_id == "taleo:acme:req:12345"


def test_global_id_keeps_special_chars() -> None:
    """Dashes, dots, slashes, parens etc. are common in ATS IDs and
    are preserved verbatim — they're meaningful, not delimiters."""
    job = _minimal_job(
        ats_type=ATSType.ICIMS, ats_id="job-2026.04/eng_(remote)"
    )
    assert job.global_id == "icims:job-2026.04/eng_(remote)"


def test_global_id_uuid_when_ats_id_none(caplog) -> None:
    import logging
    with caplog.at_level(logging.ERROR, logger="jobhive.models"):
        job = _minimal_job(ats_type=ATSType.LEVER, ats_id=None)
    assert ":" not in job.global_id  # not the formatted shape
    # Standard UUID4 string length is 36 chars (8-4-4-4-12 hex + dashes)
    assert len(job.global_id) == 36
    assert any(
        "missing/invalid ats_id" in r.getMessage() for r in caplog.records
    )


def test_global_id_uuid_when_ats_id_empty_string() -> None:
    job = _minimal_job(ats_type=ATSType.LEVER, ats_id="")
    assert ":" not in job.global_id
    assert len(job.global_id) == 36


def test_global_id_uuid_when_ats_id_only_whitespace() -> None:
    """A ats_id of just spaces is empty after strip, treat as missing."""
    job = _minimal_job(ats_type=ATSType.LEVER, ats_id="    ")
    assert ":" not in job.global_id
    assert len(job.global_id) == 36


def test_global_id_strips_trailing_crlf_and_keeps_valid() -> None:
    """\\r\\n at the end of ats_id is stripped — once gone, the
    remaining value is fine, no UUID fallback needed."""
    job = _minimal_job(ats_type=ATSType.LEVER, ats_id="abc\r\n")
    assert job.global_id == "lever:abc"
    assert job.ats_id == "abc"


@pytest.mark.parametrize("bad", ["abc\n123", "abc\tdef", "x\x00y"])
def test_global_id_uuid_when_ats_id_has_control_chars(
    bad: str, caplog
) -> None:
    """Control characters in the middle of ats_id would corrupt
    CSV/JSON output if they made it into global_id. UUID-fallback
    when present. Trailing whitespace (including \\r\\n) is stripped
    earlier and not counted as malformed."""
    import logging
    with caplog.at_level(logging.ERROR, logger="jobhive.models"):
        job = _minimal_job(ats_type=ATSType.LEVER, ats_id=bad)
    assert ":" not in job.global_id
    assert len(job.global_id) == 36
    assert any(
        "missing/invalid ats_id" in r.getMessage() for r in caplog.records
    )


def test_global_id_uniqueness_across_uuid_fallbacks() -> None:
    """Two malformed jobs must not collide on the UUID fallback."""
    j1 = _minimal_job(ats_type=ATSType.LEVER, ats_id=None,
                      url="https://x/1")
    j2 = _minimal_job(ats_type=ATSType.LEVER, ats_id=None,
                      url="https://x/2")
    assert j1.global_id != j2.global_id


def test_global_id_round_trips_through_model_dump() -> None:
    """The computed global_id is preserved across serialization, and
    re-parsing produces the same value (validator runs on validate())."""
    original = _minimal_job(ats_type=ATSType.ASHBY, ats_id="abc-123")
    assert original.global_id == "ashby:abc-123"
    payload = original.model_dump(mode="json")
    restored = Job.model_validate(payload)
    assert restored.global_id == "ashby:abc-123"


def test_global_id_user_supplied_value_is_overwritten() -> None:
    """global_id is derived. Even if a caller passes their own value,
    the validator computes a fresh one — keeps the field source-of-truth
    consistent across the codebase."""
    job = Job(
        url="https://example.com/job/1",
        title="Engineer",
        company="acme",
        ats_type=ATSType.GREENHOUSE,
        ats_id="123",
        global_id="some-bogus-value",
    )
    assert job.global_id == "greenhouse:123"
