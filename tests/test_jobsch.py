"""Tests for the jobs.ch scraper.

Pin the parsing contract, the offset-based pagination plan
(``total_hits / per_page``), and the ``_links.detail_*`` URL
preference (we want the localized URL when present, fall back to a
constructed canonical English URL otherwise).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import JobsChScraper, ScraperRegistry

_API_RE = re.compile(r"^https://www\.jobs\.ch/api/v1/public/search")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.jobsch as j
    monkeypatch.setattr(j, "MAX_RETRIES", 1)
    monkeypatch.setattr(j, "RETRY_BASE_DELAY", 0.0)


def _doc(
    *,
    job_id: str,
    title: str,
    company: str = "Acme AG",
    place: str = "Zürich",
    grades: list[int] | None = None,
    languages: list[str] | None = None,
    detail_de: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job_id,
        "title": title,
        "company_name": company,
        "company_id": 1234,
        "place": place,
        "is_active": True,
        "is_paid": True,
        "publication_date": "2026-05-07T10:57:08+02:00",
        "employment_grades": grades if grades is not None else [100],
        "language_skills": [
            {"language": lang, "level": 1} for lang in (languages or ["de"])
        ],
        "company_segmentation": "gu",
    }
    if detail_de:
        out["_links"] = {"detail_de": {"href": detail_de, "templated": False}}
    return out


def _page(docs: list[dict], total_hits: int) -> dict:
    return {"documents": docs, "total_hits": total_hits, "start": 0, "rows": 20}


# --- registry ---------------------------------------------------------------


def test_registry_resolves_jobsch() -> None:
    assert ScraperRegistry.get(ATSType.JOBSCH) is JobsChScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_doc(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(
            job_id="abc-123",
            title="Senior Software Engineer",
            company="Acme AG",
            place="Zürich",
            grades=[100],
            languages=["de", "en"],
            detail_de="https://www.jobs.ch/de/stellenangebote/detail/abc-123/",
        )], total_hits=1),
    )
    jobs = JobsChScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.JOBSCH
    assert j.ats_id == "abc-123"
    assert j.title == "Senior Software Engineer"
    assert j.company == "Acme AG"
    assert j.location == "Zürich, Switzerland"
    assert j.employment_type == "FULL_TIME"
    assert j.posted_at is not None
    assert j.raw is not None
    assert j.raw["languages"] == ["de", "en"]
    assert j.raw["employment_grades"] == [100]
    # _links.detail_de is preferred over the constructed fallback URL.
    assert str(j.url) == "https://www.jobs.ch/de/stellenangebote/detail/abc-123/"


def test_falls_back_to_canonical_url_when_no_links(httpx_mock) -> None:
    """Some rows ship without ``_links``; build a canonical URL from the
    job_id rather than emit a half-broken row."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="xyz-7", title="Engineer")], total_hits=1),
    )
    j = JobsChScraper("any").fetch()[0]
    assert str(j.url) == "https://www.jobs.ch/en/vacancies/detail/xyz-7/"


def test_part_time_employment_type(httpx_mock) -> None:
    """All grades < 100 → PART_TIME."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="p1", title="X", grades=[50])], total_hits=1),
    )
    assert JobsChScraper("any").fetch()[0].employment_type == "PART_TIME"


def test_mixed_grades_no_employment_type(httpx_mock) -> None:
    """[80, 100] is 'flexible' — leave employment_type None rather than
    pick one arbitrarily."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="p1", title="X", grades=[80, 100])], total_hits=1),
    )
    assert JobsChScraper("any").fetch()[0].employment_type is None


# --- pagination -------------------------------------------------------------


def test_paginates_total_hits_into_offsets(httpx_mock) -> None:
    """First request reads ``total_hits``; subsequent fan-out requests
    cover ``[20, 40, 60, …]`` until the total is consumed."""
    # 50 total → 3 pages of 20 (last page short).
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"a{i}", title=f"Job {i}") for i in range(20)],
                   total_hits=50),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"b{i}", title=f"Job {i}") for i in range(20)],
                   total_hits=50),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"c{i}", title=f"Job {i}") for i in range(10)],
                   total_hits=50),
    )
    jobs = JobsChScraper("any").fetch()
    assert len(jobs) == 50


def test_max_pages_truncates(httpx_mock) -> None:
    """Even if ``total_hits`` says 1000, ``max_pages=2`` must cap the
    fan-out at 1 follow-up page (probe = 1, total page-fetches = 2)."""
    # Probe page (start=0) → 20 unique ids a0..a19
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"a{i}", title="X") for i in range(20)],
                   total_hits=1000),
    )
    # Fan-out page (start=20) → 20 different ids b0..b19. After
    # max_pages=2 cap we must NOT issue a 3rd request — if we do,
    # httpx_mock errors with 'no response' for the un-stubbed call.
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"b{i}", title="X") for i in range(20)],
                   total_hits=1000),
    )
    jobs = JobsChScraper("any", max_pages=2).fetch()
    assert len(jobs) == 40


def test_no_fanout_when_total_under_per_page(httpx_mock) -> None:
    """If total_hits ≤ 20, just keep the first response and stop."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="solo", title="Only")], total_hits=1),
    )
    jobs = JobsChScraper("any").fetch()
    assert len(jobs) == 1


# --- defensive --------------------------------------------------------------


def test_skips_doc_missing_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _doc(job_id="ok", title="Good"),
            {"job_id": "no-title"},
            {"title": "no-id"},
        ], total_hits=3),
    )
    jobs = JobsChScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["ok"]


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        JobsChScraper("any").fetch()
