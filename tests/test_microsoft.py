"""Tests for the Microsoft careers scraper.

Microsoft fronts an Eightfold AI ("PCSX") tenant at
``apply.careers.microsoft.com``; the in-house ``gcsservices`` endpoint
was retired some time before 2026-05. ``MicrosoftScraper`` is a thin
wrapper around :class:`EightfoldScraper` that re-tags emitted rows
with ``ATSType.MICROSOFT`` so the dataset gets a first-class Microsoft
partition rather than burying ~1.6k jobs in the generic
``eightfold`` bucket.

These tests pin four contracts:

1. Construction defaults — the inner Eightfold scraper is wired to
   the right tenant URLs (API host vs public job-rendering host).
2. Registry — ``ATSType.MICROSOFT`` resolves to ``MicrosoftScraper``.
3. Fetch happy path — listing pages are decoded into ``Job`` rows
   tagged with ``ats_type=microsoft`` and the canonical
   ``jobs.careers.microsoft.com`` URL.
4. global_id is re-derived from the new ``ats_type`` so consumers see
   ``microsoft:<id>`` rather than ``eightfold:<id>``.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhive.models import ATSType
from jobhive.scrapers import MicrosoftScraper, ScraperRegistry
from jobhive.scrapers.microsoft import (
    API_BASE_URL,
    COMPANY_NAME,
    EIGHTFOLD_DOMAIN,
    PUBLIC_JOB_HOST,
)

# The inner Eightfold scraper fires per-job ``position_details`` GETs
# after the listing pass for description enrichment; tests that don't
# care about descriptions ignore those.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eightfold defaults to 3 retries × 1.5s base delay — kill both so
    a failing test doesn't take 9 s to surface."""
    import jobhive.scrapers.eightfold as ef
    monkeypatch.setattr(ef, "MAX_RETRIES", 1)
    monkeypatch.setattr(ef, "RETRY_BASE_DELAY", 0.0)


# --- helpers -----------------------------------------------------------------


_SEARCH_URL = f"{API_BASE_URL}/api/pcsx/search"


def _mock_url(start: int) -> str:
    return (
        f"{_SEARCH_URL}?domain={EIGHTFOLD_DOMAIN}"
        f"&query=&location=&start={start}&sort_by=timestamp"
    )


def _position(
    *,
    display_id: str = "200035593",
    title: str = "Data Center Technicians INTERN",
    location: str = "United States, Arizona, Phoenix",
    position_id: int = 1970393556860740,
    posted_ts: int = 1778547915,
) -> dict[str, Any]:
    """Real Microsoft PCSX position payload captured 2026-05-12.

    Field set mirrors what the live API actually emits — keep it close
    so regressions surface in tests when the parser drifts away from
    the on-wire shape."""
    return {
        "id": position_id,
        "displayJobId": display_id,
        "name": title,
        "locations": [location],
        "standardizedLocations": ["Phoenix, AZ, US"],
        "postedTs": posted_ts,
        "department": "Data Center Technicians",
        "creationTs": 1776716846,
        "isHot": 0,
        "workLocationOption": "onsite",
        "locationFlexibility": None,
        "atsJobId": display_id,
        "positionUrl": f"/careers/job/{position_id}",
    }


def _page(
    positions: list[dict[str, Any]], *, count: int | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "positions": positions,
            "count": count if count is not None else len(positions),
        }
    }


# --- Construction & defaults ------------------------------------------------


def test_microsoft_scraper_wires_eightfold_to_microsoft_tenant() -> None:
    """The inner Eightfold scraper must point at the Microsoft PCSX
    tenant (API host) and rewrite job URLs to the public host. These
    URL constants are the single source of truth and are exposed at
    module level so consumers (and tests) can grep for them."""
    s = MicrosoftScraper()
    assert s._inner.base_url == API_BASE_URL
    assert s._inner.domain == EIGHTFOLD_DOMAIN
    assert s._inner.company_name == COMPANY_NAME
    assert s._inner.job_url_host == PUBLIC_JOB_HOST


def test_microsoft_scraper_has_microsoft_ats_type() -> None:
    """``MicrosoftScraper`` exists exclusively to surface Microsoft as a
    first-class ATS — its ``ats`` class attribute must NOT inherit
    ``ATSType.EIGHTFOLD`` from the underlying delegate."""
    assert MicrosoftScraper.ats is ATSType.MICROSOFT


def test_microsoft_scraper_ignores_company_slug() -> None:
    """Single-tenant scraper — slug is informational. The constructor
    must accept any string without crashing and not let it bleed into
    the PCSX domain param."""
    s = MicrosoftScraper("anything-here")
    assert s.company_slug == "anything-here"
    assert s._inner.domain == EIGHTFOLD_DOMAIN  # not "anything-here.com"


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_microsoft() -> None:
    assert ScraperRegistry.get(ATSType.MICROSOFT) is MicrosoftScraper


def test_registry_resolves_microsoft_by_string() -> None:
    """The publish pipeline looks scrapers up by the string form of
    ``ATSType`` — pin that path too."""
    assert ScraperRegistry.get("microsoft") is MicrosoftScraper


# --- fetch() happy path -----------------------------------------------------


def test_fetch_single_page_emits_microsoft_tagged_jobs(httpx_mock) -> None:
    """count <= PAGE_SIZE → no fan-out. One PCSX search call yields the
    full listing; each row carries ``ats_type=microsoft``."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(display_id="200035593")], count=1),
    )
    jobs = MicrosoftScraper().fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.MICROSOFT
    assert job.company == "Microsoft"
    assert job.title == "Data Center Technicians INTERN"
    assert job.ats_id == "200035593"
    assert str(job.url).startswith(PUBLIC_JOB_HOST)


def test_fetch_global_id_uses_microsoft_prefix(httpx_mock) -> None:
    """After re-tagging, ``global_id`` must reflect the new
    ``ats_type``. Consumers route rows by the prefix — leaving it as
    ``eightfold:`` would land Microsoft jobs in the wrong partition."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(display_id="200099999")], count=1),
    )
    jobs = MicrosoftScraper().fetch()
    assert jobs[0].global_id == "microsoft:200099999"


def test_fetch_returns_empty_when_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=_mock_url(0), json=_page([], count=0))
    assert MicrosoftScraper().fetch() == []


def test_fetch_fans_out_when_count_exceeds_page_size(httpx_mock) -> None:
    """Real Microsoft listings hold ~1.6 k jobs. The Eightfold delegate
    uses ``count`` to fan out concurrent page requests — verify the
    Microsoft wrapper doesn't accidentally short-circuit that path."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [_position(display_id=f"P{i}", position_id=1000 + i) for i in range(10)],
            count=15,
        ),
    )
    httpx_mock.add_response(
        url=_mock_url(10),
        json=_page(
            [_position(display_id=f"P{i}", position_id=1000 + i) for i in range(10, 15)],
            count=15,
        ),
    )
    jobs = MicrosoftScraper().fetch()
    assert {j.ats_id for j in jobs} == {f"P{i}" for i in range(15)}
    # Every row is tagged Microsoft, never Eightfold.
    assert all(j.ats_type is ATSType.MICROSOFT for j in jobs)
    assert all(j.company == "Microsoft" for j in jobs)


def test_fetch_url_uses_public_job_host_not_api_host(httpx_mock) -> None:
    """The Eightfold response's ``positionUrl`` is relative. The
    canonical Microsoft careers URL lives on
    ``jobs.careers.microsoft.com``, not the API host — verify the
    rewrite happens for every job."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(position_id=1234567890123456)], count=1),
    )
    jobs = MicrosoftScraper().fetch()
    assert str(jobs[0].url) == (
        f"{PUBLIC_JOB_HOST}/careers/job/1234567890123456"
    )
    assert API_BASE_URL not in str(jobs[0].url)


def test_fetch_preserves_eightfold_parsed_fields(httpx_mock) -> None:
    """Sanity-check: department, location, posted_at flow through from
    the Eightfold parser. We don't re-test every field (those are
    pinned in test_eightfold.py); just verify the wrapper doesn't drop
    them."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position()], count=1),
    )
    job = MicrosoftScraper().fetch()[0]
    assert job.department == "Data Center Technicians"
    # Eightfold prefers ``standardizedLocations`` (cleaner city-state-country
    # form) over the raw ``locations`` list — pin the contract Microsoft
    # rows inherit from the underlying parser.
    assert job.location == "Phoenix, AZ, US"
    assert job.posted_at is not None
