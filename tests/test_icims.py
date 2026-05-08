"""Tests for the iCIMS scraper.

iCIMS career sites are HTML — each tenant lives at
``careers-{slug}.icims.com``. The actual listings are inside an iframe;
we hit ``/jobs/search?in_iframe=1`` directly.
"""

from __future__ import annotations

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, iCIMSScraper


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.icims as ic
    monkeypatch.setattr(ic, "MAX_RETRIES", 1)
    monkeypatch.setattr(ic, "RETRY_BASE_DELAY", 0.0)


# Per-job detail enrichment fires JSON-LD fetches against
# ``/{job}/jobs/{id}/{slug}/job?in_iframe=1`` after the listing parse.
# Tests that don't care about it leave those calls unmocked.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


def _page_url(slug: str, page: int) -> str:
    return f"https://careers-{slug}.icims.com/jobs/search?ss=1&pr={page}&in_iframe=1"


def _job_card(
    job_id: str,
    title: str,
    *,
    slug: str = "acme",
    location: str | None = None,
    posted_at: str | None = None,
    description: str | None = None,
    href: str | None = None,
    requisition_id: str | None = None,
) -> str:
    """Build an <li class="iCIMS_JobCardItem"> with the surrounding chrome
    iCIMS actually serves — the parser keys off the card boundary now, not
    the bare anchor."""
    if href is None:
        href = (
            f'https://careers-{slug}.icims.com/jobs/{job_id}/'
            f'{title.lower().replace(" ", "-")}/job?in_iframe=1'
        )
    loc_block = ""
    if location is not None:
        loc_block = (
            '<div class="col-xs-6 header left">'
            '<span class="sr-only field-label">Job Locations</span>'
            f'<span> {location}</span>'
            '</div>'
        )
    posted_block = ""
    if posted_at is not None:
        posted_block = (
            '<div class="col-xs-6 header right">'
            f'<span title="{posted_at}">label</span>'
            '</div>'
        )
    desc_block = ""
    if description is not None:
        desc_block = f'<div class="col-xs-12 description">{description}</div>'
    req_block = ""
    if requisition_id is not None:
        req_block = (
            '<div class="iCIMS_JobHeaderTag">'
            '<dt class="iCIMS_JobHeaderField">Requisition ID</dt>'
            f'<dd class="iCIMS_JobHeaderData"><span> {requisition_id}</span></dd>'
            '</div>'
        )
    return (
        '<li class="iCIMS_JobCardItem">'
        '<div class="row">'
        f'{loc_block}{posted_block}'
        '<div class="col-xs-12 title">'
        f'<a href="{href}" class="iCIMS_Anchor">'
        f'<h3>{title}</h3>'
        '</a>'
        '</div>'
        f'{desc_block}'
        f'<dl class="iCIMS_JobHeaderGroup">{req_block}</dl>'
        '</div>'
        '</li>'
    )


# Backwards-compatible alias used by existing tests that don't care about
# the surrounding card chrome — the parser still requires the <li>, but the
# helper builds a minimal one.
def _job_anchor(job_id: str, title: str, slug: str = "acme") -> str:
    return _job_card(job_id, title, slug=slug)


def _page(cards: list[str]) -> str:
    return f"<html><body><ul>{''.join(cards)}</ul></body></html>"


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_icims() -> None:
    assert ScraperRegistry.get(ATSType.ICIMS) is iCIMSScraper


# --- Construction -----------------------------------------------------------


def test_default_base_url_built_from_slug() -> None:
    s = iCIMSScraper("acme")
    assert s.base_url == "https://careers-acme.icims.com"


def test_full_url_accepted() -> None:
    s = iCIMSScraper("https://uscareers-rws.icims.com")
    assert s.base_url == "https://uscareers-rws.icims.com"


def test_company_name_derived_from_subdomain() -> None:
    """``careers-peraton.icims.com`` → ``peraton``."""
    s = iCIMSScraper("peraton")
    assert s._company_name() == "peraton"


def test_uscareers_prefix_stripped() -> None:
    s = iCIMSScraper("https://uscareers-rws.icims.com")
    assert s._company_name() == "rws"


# --- Page parsing -----------------------------------------------------------


def test_parses_basic_listing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_page_url("acme", 0),
        text=_page([
            _job_anchor("100", "Senior Engineer"),
            _job_anchor("101", "Designer"),
        ]),
    )
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert len(jobs) == 2
    assert jobs[0].ats_id == "100"
    assert jobs[0].title == "Senior Engineer"
    assert jobs[0].company == "acme"
    assert jobs[0].ats_type is ATSType.ICIMS
    assert str(jobs[0].url).startswith("https://careers-acme.icims.com/jobs/100")


def test_returns_empty_for_listing_with_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([]))
    assert iCIMSScraper("acme").fetch() == []


def test_dedupes_jobs_with_same_id(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_page_url("acme", 0),
        text=_page([
            _job_anchor("100", "Engineer"),
            _job_anchor("100", "Engineer dup"),
        ]),
    )
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert len(jobs) == 1


def test_skips_card_without_h3_title(httpx_mock) -> None:
    """A card that's just an apply button has no <h3> — drop it."""
    apply_only = (
        '<li class="iCIMS_JobCardItem">'
        '<a href="https://careers-acme.icims.com/jobs/999/apply/job?in_iframe=1" '
        'class="iCIMS_Anchor">Apply</a>'
        '</li>'
    )
    page = _page([apply_only, _job_card("100", "Real Job")])
    httpx_mock.add_response(url=_page_url("acme", 0), text=page)
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["100"]


def test_decodes_html_entities_in_url(httpx_mock) -> None:
    """iCIMS encodes special characters in slugs (`%26` for ``&``).
    The href in the rendered Job model should keep the entity decoded."""
    card = _job_card(
        "100",
        "R&amp;D Engineer",
        href="https://careers-acme.icims.com/jobs/100/r%26d-engineer/job?in_iframe=1",
    )
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert jobs[0].title == "R&D Engineer"


# --- Pagination -------------------------------------------------------------


def test_paginates_until_no_new_ids(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_page_url("acme", 0),
        text=_page([_job_anchor(str(i), f"Job {i}") for i in range(25)]),
    )
    httpx_mock.add_response(
        url=_page_url("acme", 1),
        text=_page([_job_anchor(str(i), f"Job {i}") for i in range(25, 40)]),
    )
    # Page 2 returns same as page 1 — no new IDs → terminate.
    httpx_mock.add_response(
        url=_page_url("acme", 2),
        text=_page([_job_anchor(str(i), f"Job {i}") for i in range(25)]),
    )
    jobs = iCIMSScraper("acme").fetch()
    assert len(jobs) == 40


def test_terminates_immediately_on_empty_first_page(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([]))
    assert iCIMSScraper("acme").fetch() == []


# --- Error handling ---------------------------------------------------------


def test_raises_company_not_found_on_404(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url("missing", 0), status_code=404)
    with pytest.raises(CompanyNotFoundError):
        iCIMSScraper("missing").fetch()


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.icims as ic
    monkeypatch.setattr(ic, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_page_url("acme", 0), status_code=503)
    httpx_mock.add_response(
        url=_page_url("acme", 0),
        text=_page([_job_anchor("1", "X")]),
    )
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.icims as ic
    monkeypatch.setattr(ic, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_page_url("acme", 0), status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        iCIMSScraper("acme").fetch()


# --- Location extraction (the bug this scraper used to have) ---------------


def test_extracts_location_from_card(httpx_mock) -> None:
    """The previous scraper set location=None unconditionally — that
    collapsed e.g. 943 distinct retail-merchandiser postings into one
    phantom-dup group. Locations live in the surrounding <li> card, not
    inside the anchor."""
    card = _job_card("100", "Engineer", location="US-CA-Monrovia")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    # Country-state-city is reversed to City, State, Country for readability.
    assert jobs[0].location == "Monrovia, CA, US"


def test_location_with_3_letter_country_code(httpx_mock) -> None:
    card = _job_card("100", "Engineer", location="USA-MD-Baltimore")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert jobs[0].location == "Baltimore, MD, USA"


def test_opaque_location_passes_through(httpx_mock) -> None:
    """`Remote`, `Multiple Locations` and similar free-text strings
    don't match the country-state-city dash pattern — leave them alone."""
    card = _job_card("100", "Engineer", location="Remote")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert jobs[0].location == "Remote"


def test_distinct_locations_no_longer_collapse(httpx_mock) -> None:
    """Regression for the 55%-dup-rate bug: two same-title postings at
    different stores must surface different locations."""
    cards = [
        _job_card("139835", "Retail Merchandiser", location="US-SC-Prosperity"),
        _job_card("139836", "Retail Merchandiser", location="US-NC-Charlotte"),
    ]
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page(cards))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert {j.location for j in jobs} == {"Prosperity, SC, US", "Charlotte, NC, US"}


def test_extracts_posted_at(httpx_mock) -> None:
    from datetime import datetime
    card = _job_card("100", "Engineer", posted_at="5/6/2026 10:23 AM")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert jobs[0].posted_at == datetime(2026, 5, 6, 10, 23)


def test_extracts_description(httpx_mock) -> None:
    card = _job_card("100", "Engineer", description="Build space rockets.")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert jobs[0].description == "Build space rockets."


def test_extracts_requisition_id(httpx_mock) -> None:
    card = _job_card("100", "Engineer", requisition_id="2026-100")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert jobs[0].requisition_id == "2026-100"
