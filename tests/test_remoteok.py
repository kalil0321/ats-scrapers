"""Tests for the Remote OK scraper.

Single API call, list-shaped response with metadata as the first entry.
Pin parsing, the metadata-skip behaviour, and the antibot-line stripping.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import RemoteOKScraper, ScraperRegistry


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.remoteok as ro
    monkeypatch.setattr(ro, "MAX_RETRIES", 1)
    monkeypatch.setattr(ro, "RETRY_BASE_DELAY", 0.0)


def _meta() -> dict[str, Any]:
    return {
        "last_updated": 1778083391,
        "legal": "API Terms of Service: ...",
    }


def _job(
    job_id: str = "1131473",
    *,
    position: str = "UI Developer",
    company: str = "RainFocus",
    salary_min: int | None = 100000,
    salary_max: int | None = 150000,
    location: str = "United States",
    url: str = "https://remoteok.com/remote-jobs/remote-ui-developer-1131473",
    description: str = "Build a UI.",
    epoch: int = 1777996882,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "slug": "remote-ui-developer-1131473",
        "epoch": epoch,
        "date": "2026-05-05T16:01:22+00:00",
        "company": company,
        "company_logo": "",
        "position": position,
        "tags": ["developer", "ui"],
        "description": description,
        "location": location,
        "apply_url": "https://example.com/apply",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "logo": "",
        "url": url,
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_remoteok() -> None:
    assert ScraperRegistry.get(ATSType.REMOTEOK) is RemoteOKScraper


# --- happy path -------------------------------------------------------------


def test_skips_metadata_first_entry(httpx_mock) -> None:
    """The first list entry is API metadata (no ``id``), not a job. The
    parser must skip it without confusing the job count."""
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json=[_meta(), _job(), _job(job_id="1234")],
    )
    jobs = RemoteOKScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1131473", "1234"}


def test_parses_full_payload(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json=[_meta(), _job()],
    )
    j = RemoteOKScraper("any").fetch()[0]
    assert j.ats_type is ATSType.REMOTEOK
    assert j.ats_id == "1131473"
    assert j.title == "UI Developer"
    assert j.company == "RainFocus"
    assert j.location == "United States"
    assert j.is_remote is True  # board is remote-only
    assert j.salary_currency == "USD"
    assert j.salary_min == 100000
    assert j.salary_max == 150000
    assert j.posted_at is not None
    assert j.description == "Build a UI."
    assert str(j.apply_url) == "https://example.com/apply"


def test_strips_antibot_reminder_from_description(httpx_mock) -> None:
    """Remote OK injects a 'Please mention the word X and tag Y' line into
    many descriptions. The publisher will index descriptions for search;
    keep that noise out."""
    desc = (
        "Real description here. "
        "Please mention the word **AGREEABLE** and tag RODguMTk4 when "
        "applying to show you read the job post completely "
        "(#RODguMTk4Ljk5LjE0Mw==). "
        "This is a beta feature to avoid spam applicants. Companies can "
        "search these words to find applicants that read this and see "
        "they're human."
    )
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json=[_meta(), _job(description=desc)],
    )
    j = RemoteOKScraper("any").fetch()[0]
    assert j.description is not None
    assert "Real description here." in j.description
    assert "AGREEABLE" not in j.description
    assert "beta feature" not in j.description


def test_strips_html_from_description(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json=[_meta(), _job(description="<p>Hello <b>world</b>.</p>")],
    )
    j = RemoteOKScraper("any").fetch()[0]
    assert j.description == "Hello world ."


def test_no_salary_fields_when_zero_or_missing(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json=[
            _meta(),
            _job(job_id="a", salary_min=0, salary_max=0),
            _job(job_id="b", salary_min=None, salary_max=None),
            _job(job_id="c", salary_min=80000, salary_max=120000),
        ],
    )
    by = {j.ats_id: j for j in RemoteOKScraper("any").fetch()}
    assert by["a"].salary_currency is None
    assert by["a"].salary_min is None
    assert by["b"].salary_currency is None
    assert by["c"].salary_currency == "USD"
    assert by["c"].salary_min == 80000


def test_dedupes_repeated_ids(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json=[_meta(), _job(job_id="1"), _job(job_id="1"), _job(job_id="2")],
    )
    jobs = RemoteOKScraper("any").fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2"]


# --- defensive ----------------------------------------------------------


def test_skips_entry_missing_required_fields(httpx_mock) -> None:
    """Some entries occasionally lack url/title — drop them rather than
    emit half-built rows."""
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json=[
            _meta(),
            _job(),
            {"id": "x", "company": "Acme"},  # no position, no url
            {"id": "y", "position": "Eng"},   # no url
        ],
    )
    jobs = RemoteOKScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1131473"]


def test_non_list_response_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://remoteok.com/api",
        json={"jobs": []},  # API shape changed
    )
    with pytest.raises(ScraperError, match="API shape changed"):
        RemoteOKScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://remoteok.com/api", status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        RemoteOKScraper("any").fetch()
