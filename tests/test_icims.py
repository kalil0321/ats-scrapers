"""Tests for the iCIMS scraper.

iCIMS career sites are HTML — each tenant lives at
``careers-{slug}.icims.com``. The actual listings are inside an iframe;
we hit ``/jobs/search?in_iframe=1`` directly.
"""

from __future__ import annotations

import asyncio

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, iCIMSScraper

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py — the shared fetch layer replaced per-scraper retry constants.


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


def test_configured_company_name_overrides_subdomain() -> None:
    s = iCIMSScraper(
        "https://clinical-emory.icims.com",
        company_name="Emory",
    )
    assert s._company_name() == "Emory"


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


def test_reads_past_two_hundred_pages(httpx_mock) -> None:
    for page in range(251):
        cards = [_job_anchor(str(page * 2 + i), "Job") for i in range(2)] if page < 250 else []
        httpx_mock.add_response(url=_page_url("acme", page), text=_page(cards))

    jobs = iCIMSScraper("acme", include_descriptions=False).fetch()

    assert len(jobs) == 500


def test_page_guard_raises_instead_of_truncating(httpx_mock, monkeypatch) -> None:
    from ats_scrapers.scrapers import icims

    monkeypatch.setattr(icims, "MAX_PAGES", 2)
    for page in range(2):
        httpx_mock.add_response(
            url=_page_url("acme", page), text=_page([_job_anchor(str(page), "Job")])
        )

    with pytest.raises(ScraperError, match="after 2 pages"):
        iCIMSScraper("acme", include_descriptions=False).fetch()


def test_terminates_immediately_on_empty_first_page(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([]))
    assert iCIMSScraper("acme").fetch() == []


# --- Error handling ---------------------------------------------------------


def test_raises_company_not_found_on_404(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url("missing", 0), status_code=404)
    with pytest.raises(CompanyNotFoundError):
        iCIMSScraper("missing").fetch()


_PASSWORD_PAGE = (
    "<html><head><title>iCIMS Portal</title></head><body><form method=\"POST\">"
    "<label for=\"loginToAccessPortalPassword\">Password</label>"
    "<input type=\"password\" id=\"loginToAccessPortalPassword\" "
    "name=\"loginToAccessPortalPassword\" /></form></body></html>"
)


@pytest.mark.parametrize("status", [403, 503])
def test_portal_behind_a_password_is_not_found(httpx_mock, status) -> None:
    httpx_mock.add_response(url=_page_url("acme", 0), status_code=status, text=_PASSWORD_PAGE)
    with pytest.raises(CompanyNotFoundError, match="password"):
        iCIMSScraper("acme").fetch()
    assert len(httpx_mock.get_requests()) == 1


def test_other_403_stays_an_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_page_url("acme", 0), status_code=403, text="Forbidden", is_reusable=True
    )
    with pytest.raises(ScraperError, match="403") as exc_info:
        iCIMSScraper("acme").fetch()
    assert not isinstance(exc_info.value, CompanyNotFoundError)


def test_5xx_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url("acme", 0), status_code=503)
    httpx_mock.add_response(
        url=_page_url("acme", 0),
        text=_page([_job_anchor("1", "X")]),
    )
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(httpx_mock) -> None:
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


# A search card and the JSON-LD of its posting, as careers-hhsys.icims.com
# serves them (trimmed): the card holds the overview's first sentence only.
_HHSYS_JOB_URL = (
    "https://careers-hhsys.icims.com/jobs/77260/cln-precertification-rep/job?in_iframe=1"
)
_HHSYS_CARD = f"""<li class="iCIMS_JobCardItem">
<div class="row">
<div class="col-xs-6 header left">
<span class="sr-only field-label">Job Locations</span>
<span >
US-AL-Huntsville</span>
</div>
<div class="col-xs-12 title">
<a href="{_HHSYS_JOB_URL}" class="iCIMS_Anchor" title="77260 - CLN Precertification Rep">
<span class="sr-only field-label">Requisition Title</span>
<h3 >
CLN Precertification Rep</h3>
</a>
</div>
<div class="col-xs-12 description">
Responsible for reviewing requests for services that require pre-certification and or referrals.</div>
</div>
</li>"""
_HHSYS_DETAIL = (
    '<script type="application/ld+json">{"hiringOrganization":{"@type":"Organization",'
    '"name":"Huntsville Hospital Health System"},"employmentType":"OTHER",'
    '"@type":"JobPosting","description":"<h2>Overview<\\/h2>\\n<p>Responsible for '
    "reviewing requests for services that require pre-certification and or referrals."
    '<strong>&nbsp;<\\/strong><\\/p>\\n<h2>Qualifications<\\/h2>\\n<p><strong>Education:'
    "&nbsp; <\\/strong>High School graduate of GED required.<\\/p>\\n<p><strong>Experience:"
    "&nbsp; <\\/strong>Three years of insurance and/or billing experience including one "
    'year of experience in a health care organization preferred.<\\/p>",'
    '"title":"CLN Precertification Rep"}</script>'
)


def test_the_posting_replaces_the_search_card_summary(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url("hhsys", 0), text=_page([_HHSYS_CARD]))
    httpx_mock.add_response(url=_page_url("hhsys", 1), text=_page([]))
    httpx_mock.add_response(url=_HHSYS_JOB_URL, text=_HHSYS_DETAIL)

    [job] = iCIMSScraper("hhsys", company_name="Huntsville Hospital").fetch()

    assert job.description == (
        "Overview Responsible for reviewing requests for services that require "
        "pre-certification and or referrals. Qualifications Education: High School "
        "graduate of GED required. Experience: Three years of insurance and/or billing "
        "experience including one year of experience in a health care organization "
        "preferred."
    )


def test_a_card_summary_longer_than_the_posting_is_kept(httpx_mock) -> None:
    card = _job_card("100", "Engineer", description="Build space rockets for a living.")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    httpx_mock.add_response(
        url="https://careers-acme.icims.com/jobs/100/engineer/job?in_iframe=1",
        text='<script type="application/ld+json">'
        '{"@type": "JobPosting", "description": "<p>Rockets.</p>"}</script>',
    )

    [job] = iCIMSScraper("acme").fetch()

    assert job.description == "Build space rockets for a living."


def test_extracts_requisition_id(httpx_mock) -> None:
    card = _job_card("100", "Engineer", requisition_id="2026-100")
    httpx_mock.add_response(url=_page_url("acme", 0), text=_page([card]))
    httpx_mock.add_response(url=_page_url("acme", 1), text=_page([]))
    jobs = iCIMSScraper("acme").fetch()
    assert jobs[0].requisition_id == "2026-100"


# --- employer name from the detail page -----------------------------------------

def _detail_page(organization: object) -> str:
    import json as _json
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Nurse",
               "hiringOrganization": organization}
    return f'<script type="application/ld+json">{_json.dumps(posting)}</script>'


def _icims_job(company: str):
    from ats_scrapers.models import ATSType, Job
    return Job(url="https://careers-daytonfreight.icims.com/jobs/1/nurse/job", title="Nurse",
               company=company, ats_type=ATSType.ICIMS, ats_id="1")


@pytest.mark.parametrize(("organization", "expected"), [
    ({"@type": "Organization", "name": "Dayton Freight"}, "Dayton Freight"),
    ({"@type": "Organization", "name": " Dayton  Freight "}, "Dayton Freight"),
    ({"@type": "Organization", "name": "UNAVAILABLE"}, "daytonfreight"),
    ({"@type": "Organization", "name": ""}, "daytonfreight"),
    ("Dayton Freight", "daytonfreight"),
    (None, "daytonfreight"),
])
def test_host_label_company_takes_the_hiring_organization(organization, expected) -> None:
    from ats_scrapers.scrapers.icims import _apply_jsonld_to_job

    job = _icims_job("daytonfreight")
    _apply_jsonld_to_job(job, _detail_page(organization), replace_company=True)
    assert job.company == expected


def test_a_given_company_name_is_kept() -> None:
    from ats_scrapers.scrapers.icims import _apply_jsonld_to_job

    job = _icims_job("bcore")
    _apply_jsonld_to_job(job, _detail_page({"@type": "Organization", "name": "Explore"}))
    assert job.company == "bcore"


@pytest.mark.parametrize(("slug", "company_name", "is_host_label"), [
    ("daytonfreight", None, True),
    ("retail-shaneco", "retail-shaneco", True),
    ("https://retail-shaneco.icims.com", "Retail-Shaneco", True),
    ("https://uscareers-rws.icims.com", "rws", True),
    ("explore", "bcore", False),
    ("clinical-acme", "acme", False),
    ("daytonfreight", "Dayton Freight", False),
])
def test_which_names_stand_in_for_a_missing_one(slug, company_name, is_host_label) -> None:
    assert iCIMSScraper(slug, company_name=company_name)._name_is_host_label() is is_host_label


def test_detail_pages_name_the_employer_of_a_host_label_board(httpx_mock) -> None:
    scraper = iCIMSScraper("daytonfreight", company_name="daytonfreight")
    job = _icims_job("daytonfreight")
    httpx_mock.add_response(url=str(job.url), text=_detail_page({"@type": "Organization", "name": "Dayton Freight"}))
    asyncio.run(_enrich_one(scraper, job))
    assert job.company == "Dayton Freight"

    named = iCIMSScraper("daytonfreight", company_name="DF Lines")
    other = _icims_job("DF Lines")
    httpx_mock.add_response(url=str(other.url), text=_detail_page({"@type": "Organization", "name": "Dayton Freight"}))
    asyncio.run(_enrich_one(named, other))
    assert other.company == "DF Lines"


async def _enrich_one(scraper, job) -> None:
    async with scraper.make_fetcher() as fetch:
        await scraper._enrich_detail(fetch, asyncio.Semaphore(1), job)
