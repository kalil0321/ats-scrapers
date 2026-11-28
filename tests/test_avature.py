"""Tests for the Avature scraper.

Avature pages are server-rendered HTML and the markup varies significantly
between tenants — Bloomberg uses ``article.article--result``, IBM uses
``div.job-item``, Astellas uses table rows. The scraper handles this by
finding `/JobDetail/` anchors and walking up to the wrapping container,
then extracting siblings.

These tests pin:

1. Pagination via ``jobOffset/jobRecordsPerPage`` (the working scheme;
   the old ``pageNumber=N`` was broken on most tenants).
2. Multi-tenant HTML support (Bloomberg, IBM, Astellas, anchor-only).
3. Pseudo-anchor filtering ("Apply", "Save", "Learn More").
4. Retry behaviour and 404 fail-fast.
5. Locale path support (``careers.ibm.com/en_US/...``).
"""

from __future__ import annotations

import html
import json
import logging
import socket
import sys
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl

import httpx
import pytest

from ats_scrapers import CloudBrowser, RenderedPage, get_scraper_for_url
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import AvatureScraper, ScraperRegistry, get_scraper
from ats_scrapers.scrapers.avature import _paginated_search_url


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 1)
    monkeypatch.setattr(av, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(av, "RATE_LIMIT_WAITS", (0.0, 0.0))


# Avature now enriches each job with a per-job ``/careers/JobDetail/...``
# fetch (best-effort). Tests that don't care about description/metadata
# leave those calls unmocked — relax the unmatched-request check at the
# module level so they don't have to enumerate every detail URL.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


def _url(slug: str, offset: int) -> str:
    return (
        f"https://{slug}.avature.net/careers/SearchJobs/"
        f"?jobOffset={offset}&jobRecordsPerPage=12"
    )


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_avature() -> None:
    assert ScraperRegistry.get(ATSType.AVATURE) is AvatureScraper


def test_get_scraper_returns_avature() -> None:
    s = get_scraper("avature", "bloomberg")
    assert isinstance(s, AvatureScraper)


# --- Pagination URL form ----------------------------------------------------


def test_uses_joboffset_pagination_not_pagenumber(httpx_mock) -> None:
    """Pagination MUST be ``jobOffset/jobRecordsPerPage`` — the legacy
    ``pageNumber=N`` query was broken on most real tenants."""
    httpx_mock.add_response(
        url=_url("acme", 0),
        text='<a href="/careers/JobDetail/Engineer/1">Engineer</a>',
    )
    jobs = AvatureScraper("acme").fetch()
    assert len(jobs) == 1


# --- Tenant-specific markup -------------------------------------------------


BLOOMBERG_PAGE = """
<html><body>
<article class="article article--result" id="article--1">
  <div class="article__header">
    <div class="article__header__text">
      <h3 class="article__header__text__title title">
        <a class="link" href="https://acme.avature.net/careers/JobDetail/Streaming-Operator/19445">
          Streaming Transmissions Operator - Contract
        </a>
      </h3>
      <div class="article__header__text__subtitle">
        <span class="list-item-location">New York, NY, United States</span>
      </div>
    </div>
  </div>
  <div class="article__footer">
    <a class="button button--primary" href="https://acme.avature.net/careers/JobDetail/Streaming-Operator/19445">
      Apply
    </a>
  </div>
</article>
</body></html>
"""

IBM_PAGE = """
<html><body>
<div class="job-item" data-job-id="42">
  <h2 class="job-title">
    <a href="/careers/JobDetail/Senior-Engineer/9001">Senior Engineer</a>
  </h2>
  <span class="job-location">Armonk, NY</span>
  <span class="department">Cloud Platform</span>
</div>
</body></html>
"""

ANCHOR_ONLY_PAGE = """
<html><body>
  <a href="/careers/JobDetail/Designer/2">Product Designer</a>
  <a href="/careers/JobDetail/PM/3">Product Manager</a>
</body></html>
"""


def test_parses_bloomberg_style_article(httpx_mock) -> None:
    """Bloomberg markup uses ``article.article--result`` with title in
    ``.article__header__text__title`` and location in
    ``.list-item-location``. Extracting all three is essential for
    Bloomberg-scale tenants (~500 jobs)."""
    httpx_mock.add_response(url=_url("acme", 0), text=BLOOMBERG_PAGE)
    jobs = AvatureScraper("acme").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Streaming Transmissions Operator - Contract"
    assert jobs[0].location == "New York, NY, United States"
    assert jobs[0].ats_id == "19445"
    assert str(jobs[0].url) == "https://acme.avature.net/careers/JobDetail/Streaming-Operator/19445"


def test_parses_ibm_style_div_with_data_job_id(httpx_mock) -> None:
    httpx_mock.add_response(url=_url("acme", 0), text=IBM_PAGE)
    jobs = AvatureScraper("acme").fetch()
    assert jobs[0].title == "Senior Engineer"
    assert jobs[0].location == "Armonk, NY"
    assert jobs[0].department == "Cloud Platform"
    assert jobs[0].ats_id == "9001"


def test_parses_anchor_only_fallback_markup(httpx_mock) -> None:
    """If the page has no recognized wrapper element (very old tenants),
    just the anchors must still produce jobs."""
    httpx_mock.add_response(url=_url("acme", 0), text=ANCHOR_ONLY_PAGE)
    jobs = AvatureScraper("acme").fetch()
    assert {j.title for j in jobs} == {"Product Designer", "Product Manager"}
    assert {j.location for j in jobs} == {None}
    assert {j.ats_id for j in jobs} == {"2", "3"}


# --- Pseudo-anchor filtering ------------------------------------------------


def test_skips_apply_button_anchor(httpx_mock) -> None:
    """Bloomberg's ``article__footer`` has TWO ``/JobDetail/`` anchors per
    job: the title link and an "Apply" button. We must dedupe, NOT emit the
    job twice and NOT use "Apply" as a title."""
    httpx_mock.add_response(url=_url("acme", 0), text=BLOOMBERG_PAGE)
    jobs = AvatureScraper("acme").fetch()
    assert len(jobs) == 1
    assert jobs[0].title != "Apply"


def test_pseudo_only_anchors_are_filtered(httpx_mock) -> None:
    """A pseudo-anchor with no real title context should be dropped."""
    page = """
    <html><body>
      <a href="/careers/JobDetail/Apply/123">Apply</a>
      <a href="/careers/JobDetail/Real/456">Real Engineer</a>
    </body></html>
    """
    httpx_mock.add_response(url=_url("acme", 0), text=page)
    jobs = AvatureScraper("acme").fetch()
    assert [j.title for j in jobs] == ["Real Engineer"]


def test_parses_job_detail_links_keyed_by_job_id_query(httpx_mock) -> None:
    """Booz Allen links jobs as ``JobDetail?jobId=N``."""
    page = """
    <html><body>
    <article class="article article--result">
      <h3 class="article__header__text__title">
        <a href="https://acme.avature.net/careers/JobDetail?jobId=132998">Data Scientist</a>
      </h3>
      <a href="https://acme.avature.net/careers/ApplicationMethods?jobId=132998">Apply</a>
    </article>
    </body></html>
    """
    httpx_mock.add_response(url=_url("acme", 0), text=page)

    [job] = AvatureScraper("acme").fetch()

    assert job.ats_id == "132998"
    assert job.title == "Data Scientist"
    assert str(job.url) == "https://acme.avature.net/careers/JobDetail?jobId=132998"


def test_parses_folder_detail_links_and_pages_by_folder_offset(httpx_mock) -> None:
    """Epic, Rosewood and others link jobs as ``FolderDetail`` pages."""
    def page(start: int, count: int, link: str = "") -> str:
        anchors = "".join(
            f'<article class="article article--result"><h3><a href='
            f'"https://acme.avature.net/careers/FolderDetail/Job-{i}/{i}">Job {i}</a>'
            f"</h3></article>"
            for i in range(start, start + count)
        )
        return f"<html><body>{anchors}{link}</body></html>"

    httpx_mock.add_response(
        url=_url("acme", 0),
        text=page(
            0, 10,
            '<a class="paginationNextLink" href="https://acme.avature.net/careers/'
            'SearchJobs/?folderRecordsPerPage=10&amp;folderOffset=10">Next</a>',
        ),
    )
    httpx_mock.add_response(
        url="https://acme.avature.net/careers/SearchJobs/?folderRecordsPerPage=10&folderOffset=10",
        text=page(10, 3),
    )

    jobs = AvatureScraper("acme").fetch()

    assert [j.ats_id for j in jobs] == [str(i) for i in range(13)]
    assert jobs[0].title == "Job 0"


def test_parses_a_renamed_detail_page(httpx_mock) -> None:
    page = """
    <html><body><article class="article article--result">
      <h3><a class="link" href="https://acme.avature.net/careers/Job-Description/Analyst-Albany-NY/44970">Analyst</a></h3>
    </article></body></html>
    """
    httpx_mock.add_response(url=_url("acme", 0), text=page)

    [job] = AvatureScraper("acme").fetch()

    assert (job.ats_id, job.title) == ("44970", "Analyst")


def test_share_links_are_not_jobs(httpx_mock) -> None:
    page = """
    <html><body><article class="article article--result">
      <h3><a href="https://acme.avature.net/careers/JobDetail/Crew/21590">Crew</a></h3>
      <a href="mailto:?body=https://acme.avature.net/careers/JobDetail/Crew/21590">Share</a>
      <a href="https://twitter.com/intent/tweet?url=https://acme.avature.net/careers/JobDetail/Crew/21590">Tweet</a>
    </article></body></html>
    """
    httpx_mock.add_response(url=_url("acme", 0), text=page)

    jobs = AvatureScraper("acme").fetch()

    assert [j.ats_id for j in jobs] == ["21590"]


# --- Pages without job links ------------------------------------------------


def test_board_that_says_it_has_no_jobs_is_empty(httpx_mock) -> None:
    page = """
    <html><head><link href="/ASSET/portal/avature.css" rel="stylesheet"></head><body>
      <div class="list-controls__text__legend" aria-label="0 results"></div>
      <h3 class="article__header__text__title">No jobs found</h3>
    </body></html>
    """
    httpx_mock.add_response(url=_url("acme", 0), text=page)

    assert AvatureScraper("acme").fetch() == []


@pytest.mark.parametrize(
    "count",
    [
        '<div class="list-controls__text__legend" aria-label="432 results"></div>',
        '<div aria-label="432 results" class="list-controls__text__legend"></div>',
        '<span class="jobPaginationLegend">1-20 of 169 results</span>',
    ],
)
def test_listing_with_results_but_no_readable_job_raises(httpx_mock, count: str) -> None:
    page = f"""
    <html><body><p>Powered by Avature</p>{count}
      <a href="https://acme.avature.net/careers/Unknown-Page/Analyst/44970">Analyst</a>
    </body></html>
    """
    httpx_mock.add_response(url=_url("acme", 0), text=page)

    with pytest.raises(ScraperError, match="no job link could be read"):
        AvatureScraper("acme").fetch()


def test_board_that_redirects_off_avature_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_url("acme", 0), status_code=301, headers={"Location": "https://careers.acme.example/en"},
    )
    httpx_mock.add_response(
        url="https://careers.acme.example/en", text="<html><body>Search jobs</body></html>",
    )

    with pytest.raises(CompanyNotFoundError, match=r"careers\.acme\.example"):
        AvatureScraper("acme").fetch()


def test_page_that_is_not_an_avature_listing_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_url("acme", 0), text="<html><body>Maintenance</body></html>")

    with pytest.raises(ScraperError, match="not an Avature listing") as excinfo:
        AvatureScraper("acme").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


# --- Pagination & termination ----------------------------------------------


def test_paginates_through_full_set(httpx_mock) -> None:
    """Three pages: 12 jobs, 12 jobs, 5 jobs (short — terminates)."""
    def mkpage(start: int, count: int) -> str:
        anchors = [
            f'<a href="/careers/JobDetail/Job/{i}">Job {i}</a>'
            for i in range(start, start + count)
        ]
        return f"<html><body>{''.join(anchors)}</body></html>"

    httpx_mock.add_response(url=_url("acme", 0), text=mkpage(0, 12))
    httpx_mock.add_response(url=_url("acme", 12), text=mkpage(12, 12))
    httpx_mock.add_response(url=_url("acme", 24), text=mkpage(24, 5))
    jobs = AvatureScraper("acme").fetch()
    assert len(jobs) == 29
    assert {j.ats_id for j in jobs} == {str(i) for i in range(29)}


def test_pagination_dedupes_overlapping_pages(httpx_mock) -> None:
    """If a tenant returns the same job in two consecutive pages (rare —
    listing shifts mid-scrape), output keeps each job once. Page 1 must be
    a full 12 to actually trigger the page-2 fetch."""
    page_1 = "".join(
        f'<a href="/careers/JobDetail/Job/{i}">Job {i}</a>' for i in range(12)
    )
    # Page 2 has 11 entries: 8 dupes from page 1 + 3 new jobs (12-15).
    page_2 = "".join(
        f'<a href="/careers/JobDetail/Job/{i}">Job {i}</a>' for i in [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    )
    httpx_mock.add_response(url=_url("acme", 0), text=page_1)
    httpx_mock.add_response(url=_url("acme", 12), text=page_2)
    jobs = AvatureScraper("acme").fetch()
    assert {j.ats_id for j in jobs} == {str(i) for i in range(15)}


def test_full_page_followed_by_empty_terminates(httpx_mock) -> None:
    """A full page-1 + empty page-2 must terminate. Without this, the
    bounded MAX_PAGES is the only safety net — too easy to runaway."""
    page_1 = "".join(
        f'<a href="/careers/JobDetail/Job/{i}">Job {i}</a>' for i in range(12)
    )
    httpx_mock.add_response(url=_url("acme", 0), text=page_1)
    httpx_mock.add_response(url=_url("acme", 12), text="<html></html>")
    jobs = AvatureScraper("acme").fetch()
    assert len(jobs) == 12


def _linked_pages(httpx_mock, first_url: str, page_url: str, per_page: int, total: int) -> None:
    """Pages of ``per_page`` jobs, each linking to the next at ``page_url``
    (formatted with the offset) and echoing its own offset in a language
    link, as Avature portals do."""
    for offset in range(0, total, per_page):
        anchors = "".join(
            f'<a href="/careers/JobDetail/Job/{i}">Job {i}</a>'
            for i in range(offset, min(offset + per_page, total))
        )
        own = page_url.format(offset=offset).replace("&", "&amp;")
        nxt = ""
        if offset + per_page < total:
            link = page_url.format(offset=offset + per_page).replace("&", "&amp;")
            nxt = f'<a class="paginationNextLink" href="{link}">Next</a>'
        httpx_mock.add_response(
            url=first_url if offset == 0 else page_url.format(offset=offset),
            text=f'<html><body><a href="{own}">FR</a>{anchors}{nxt}</body></html>',
        )


def test_portal_serving_fewer_rows_than_asked_is_read_to_the_end(httpx_mock) -> None:
    """careers.avature.net answers ``jobRecordsPerPage=12`` with 6 rows and
    links to the next page at ``jobOffset=6``."""
    _linked_pages(
        httpx_mock,
        _url("acme", 0),
        "https://acme.avature.net/careers/SearchJobs/?jobRecordsPerPage=6&jobOffset={offset}",
        per_page=6,
        total=19,
    )

    jobs = AvatureScraper("acme").fetch()

    assert {j.ats_id for j in jobs} == {str(i) for i in range(19)}


def test_portal_paging_by_project_offset_is_read_to_the_end(httpx_mock) -> None:
    _linked_pages(
        httpx_mock,
        _url("acme", 0),
        "https://acme.avature.net/careers/SearchJobs/?projectRecordsPerPage=10&projectOffset={offset}",
        per_page=10,
        total=25,
    )

    jobs = AvatureScraper("acme").fetch()

    assert len(jobs) == 25


def test_pages_follow_the_host_a_portal_redirects_to(httpx_mock) -> None:
    """The redirect drops ``jobOffset``; the page's links carry it."""
    _linked_pages(
        httpx_mock,
        _url("acme", 0),
        "https://jobs.acme.example/en_US/SearchJobs/?jobRecordsPerPage=10&jobOffset={offset}",
        per_page=10,
        total=25,
    )

    jobs = AvatureScraper("acme").fetch()

    assert len(jobs) == 25


def test_page_guard_raises_instead_of_truncating(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.avature as av

    monkeypatch.setattr(av, "MAX_PAGES", 2)
    for offset in (0, 12):
        anchors = "".join(
            f'<a href="/careers/JobDetail/Job/{i}">Job {i}</a>'
            for i in range(offset, offset + 12)
        )
        httpx_mock.add_response(url=_url("acme", offset), text=f"<html><body>{anchors}</body></html>")

    with pytest.raises(ScraperError, match="after 2 pages"):
        AvatureScraper("acme").fetch()


def test_relative_page_links_follow_the_host_that_served_the_page(httpx_mock) -> None:
    """After the redirect, a relative link names a path on the new host."""
    new_search = "https://jobs.acme.example/en_US/SearchJobs/"

    def page(start: int, count: int, link: str = "") -> str:
        anchors = "".join(
            f'<a href="https://jobs.acme.example/en_US/JobDetail/Job/{i}">Job {i}</a>'
            for i in range(start, start + count)
        )
        return f"<html><body>{anchors}{link}</body></html>"

    httpx_mock.add_response(
        url=_url("acme", 0), status_code=302, headers={"Location": new_search},
    )
    httpx_mock.add_response(
        url=new_search,
        text=page(0, 10, '<a href="/en_US/SearchJobs/?jobRecordsPerPage=10&amp;jobOffset=10">2</a>'),
    )
    httpx_mock.add_response(
        url=f"{new_search}?jobRecordsPerPage=10&jobOffset=10", text=page(10, 5),
    )

    jobs = AvatureScraper("acme").fetch()

    assert len(jobs) == 15


# --- URL forms --------------------------------------------------------------


def test_full_base_url_is_supported_for_custom_domain_tenants(httpx_mock) -> None:
    """IBM and similar custom-domain tenants pass the full base URL as
    ``company_slug``. The scraper must use it as-is rather than guessing
    `careers.ibm.com.avature.net`."""
    base = "https://careers.example.com/en_US"
    url = (
        f"{base}/careers/SearchJobs/?jobOffset=0&jobRecordsPerPage=12"
    )
    httpx_mock.add_response(
        url=url, text='<a href="/careers/JobDetail/Eng/1">Eng</a>',
    )
    jobs = AvatureScraper(base).fetch()
    assert len(jobs) == 1
    assert str(jobs[0].url) == "https://careers.example.com/en_US/careers/JobDetail/Eng/1"


def test_full_base_url_with_custom_search_path_is_supported(httpx_mock) -> None:
    """Some custom-domain tenants use `/jobs/SearchJobs` or other portal
    prefixes instead of the default `/careers/SearchJobs`."""
    base = "https://jobs.example.com/jobs"
    httpx_mock.add_response(
        url=f"{base}/SearchJobs/?jobOffset=0&jobRecordsPerPage=12",
        text='<a href="/jobs/ProjectDetail/Eng/1">Eng</a>',
    )

    jobs = AvatureScraper(base).fetch()

    assert len(jobs) == 1
    assert str(jobs[0].url) == "https://jobs.example.com/jobs/ProjectDetail/Eng/1"


def test_full_searchjobs_url_is_supported(httpx_mock) -> None:
    base = "https://company.example.com/custom/SearchJobs"
    httpx_mock.add_response(
        url=f"{base}/?jobOffset=0&jobRecordsPerPage=12",
        text='<a href="/custom/JobDetail/Eng/1">Eng</a>',
    )

    jobs = AvatureScraper(base).fetch()

    assert len(jobs) == 1
    assert str(jobs[0].url) == "https://company.example.com/custom/JobDetail/Eng/1"


def test_searchjobs_maps_pipeline_variant_is_supported(httpx_mock) -> None:
    base = "https://company.example.com/en_US/jobs/SearchJobsMaps"
    httpx_mock.add_response(
        url=f"{base}/?pipelineOffset=0",
        text=(
            '<li class="list__item">'
            '<div class="list__item__text__title">'
            '<a href="https://company.example.com/en_US/jobs/'
            'PipelineDetail?pipelineId=123">Retail Specialist</a>'
            "</div>"
            '<div class="list__item__text__subtitle">'
            "<span>Chicago, Illinois, United States</span>"
            "</div>"
            "</li>"
        ),
    )

    jobs = AvatureScraper(base).fetch()

    assert len(jobs) == 1
    assert jobs[0].ats_id == "123"
    assert jobs[0].title == "Retail Specialist"
    assert str(jobs[0].url) == (
        "https://company.example.com/en_US/jobs/PipelineDetail?pipelineId=123"
    )


def test_searchjobs_maps_direct_pagination_uses_pipeline_offsets(httpx_mock) -> None:
    base = "https://company.example.com/en_US/jobs/SearchJobsMaps"

    def mkpage(start: int, count: int) -> str:
        return "".join(
            "<li class='list__item'>"
            "<div class='list__item__text__title'>"
            f"<a href='/en_US/jobs/PipelineDetail?pipelineId={i}'>Job {i}</a>"
            "</div>"
            "<div class='list__item__text__subtitle'><span>Remote</span></div>"
            "</li>"
            for i in range(start, start + count)
        )

    httpx_mock.add_response(url=f"{base}/?pipelineOffset=0", text=mkpage(0, 30))
    httpx_mock.add_response(url=f"{base}/?pipelineOffset=30", text=mkpage(30, 1))

    jobs = AvatureScraper(base).fetch()

    assert len(jobs) == 31
    assert jobs[-1].ats_id == "30"
    assert str(jobs[0].url) == (
        "https://company.example.com/en_US/jobs/PipelineDetail?pipelineId=0"
    )


def test_searchjobs_maps_query_params_are_preserved(httpx_mock) -> None:
    base = "https://company.example.com/en_US/jobs/SearchJobsMaps?folderId=abc&lang=en"
    expected_url = (
        "https://company.example.com/en_US/jobs/SearchJobsMaps/"
        "?folderId=abc&lang=en&pipelineOffset=0"
    )
    httpx_mock.add_response(
        url=expected_url,
        text=(
            "<li class='list__item'>"
            "<div class='list__item__text__title'>"
            "<a href='/en_US/jobs/PipelineDetail?pipelineId=123'>Retail Specialist</a>"
            "</div>"
            "</li>"
        ),
    )

    jobs = AvatureScraper(base).fetch()

    assert len(jobs) == 1
    assert str(jobs[0].url) == (
        "https://company.example.com/en_US/jobs/PipelineDetail?pipelineId=123"
    )


def test_paginated_search_url_preserves_searchjobs_maps_query_params() -> None:
    base = "https://company.example.com/en_US/jobs/SearchJobsMaps?folderId=abc&lang=en"

    url = _paginated_search_url(base, 30)

    assert url == (
        "https://company.example.com/en_US/jobs/SearchJobsMaps/"
        "?folderId=abc&lang=en&pipelineOffset=30"
    )


# --- Error handling --------------------------------------------------------


def test_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=_url("missing", 0), status_code=404)
    with pytest.raises(CompanyNotFoundError):
        AvatureScraper("missing").fetch()


def test_404_does_not_retry(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_url("missing", 0), status_code=404)
    with pytest.raises(CompanyNotFoundError):
        AvatureScraper("missing").fetch()


def _dns_error(errno: int) -> httpx.ConnectError:
    exc = httpx.ConnectError(f"[Errno {errno}] DNS lookup failed")
    exc.__cause__ = socket.gaierror(errno, "DNS lookup failed")
    return exc


@pytest.mark.parametrize(
    "errno", [socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)],
)
def test_unresolvable_host_raises_company_not_found(
    monkeypatch, httpx_mock, errno,
) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    httpx_mock.add_exception(_dns_error(errno), url=_url("gone", 0))
    with pytest.raises(CompanyNotFoundError, match="does not resolve"):
        AvatureScraper("gone").fetch()


def _dns(monkeypatch, missing: tuple[str, ...] = (), errno: int = socket.EAI_NONAME) -> list[str]:
    """Answer lookups without the network: hosts in ``missing`` have no address."""
    import ats_scrapers.scrapers.avature as av
    asked: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        if host in missing or "*" in missing:
            raise socket.gaierror(errno, "no address")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]

    monkeypatch.setattr(av.socket, "getaddrinfo", getaddrinfo)
    return asked


def test_failed_lookup_behind_an_environment_proxy_is_not_not_found(
    monkeypatch, httpx_mock,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    _dns(monkeypatch)
    httpx_mock.add_exception(_dns_error(socket.EAI_NONAME), url=_url("acme", 0), is_reusable=True)
    with pytest.raises(ScraperError) as excinfo:
        AvatureScraper("acme").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_transient_dns_failure_stays_a_scraper_error(httpx_mock) -> None:
    httpx_mock.add_exception(_dns_error(socket.EAI_AGAIN), url=_url("acme", 0))
    with pytest.raises(ScraperError) as excinfo:
        AvatureScraper("acme").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_aws_waf_challenge_is_reported_without_retrying(
    monkeypatch, httpx_mock,
) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    httpx_mock.add_response(
        url=_url("acme", 0),
        status_code=202,
        headers={"x-amzn-waf-action": "challenge"},
        text="<html><script>window.gokuProps = {};</script></html>",
    )
    with pytest.raises(ScraperError, match=r"AWS WAF.*cloud_browser"):
        AvatureScraper("acme").fetch()


@pytest.fixture
def refused_to_httpcloak(monkeypatch: pytest.MonkeyPatch, httpx_mock) -> None:
    """The listing answers 406 to httpx and to httpcloak."""
    import ats_scrapers.scrapers.avature as av

    async def refused(self, base: str, company: str) -> list:
        raise av._BlockedTenantError(base)

    monkeypatch.setattr(av.AvatureScraper, "_fetch_via_httpcloak", refused)
    httpx_mock.add_response(url=_url("acme", 0), status_code=406)


class FakeBrowser(CloudBrowser):
    """Serves ``pages`` by URL and raises for any other address."""

    def __init__(self, pages: dict[str, RenderedPage]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    async def render(self, url: str) -> RenderedPage:
        self.requested.append(url)
        if url not in self.pages:
            raise RuntimeError(f"no page at {url}")
        return self.pages[url]


def _detail_page(text: str) -> str:
    return (
        '<div class="article--details"><div class="article__content__view__field">'
        f'<div class="article__content__view__field__value">{text}</div>'
        "</div></div>"
    )


def test_refused_board_without_a_cloud_browser_raises(refused_to_httpcloak) -> None:
    with pytest.raises(ScraperError, match=r"refused the listing \(406\).*cloud_browser"):
        AvatureScraper("acme").fetch()


def test_refused_board_is_read_through_the_cloud_browser(refused_to_httpcloak) -> None:
    jobs_url = "https://acme.avature.net/en_US/careers"
    browser = FakeBrowser({
        # Redirected: the next-page link is relative to the address that answered.
        _url("acme", 0): RenderedPage(
            html=(
                f'<a href="{jobs_url}/JobDetail/Engineer/1">Engineer</a>'
                f'<a href="{jobs_url}/JobDetail/Designer/2">Designer</a>'
                '<a href="?jobOffset=12&amp;jobRecordsPerPage=12">Next</a>'
            ),
            url=f"{jobs_url}/SearchJobs/?jobOffset=0",
        ),
        f"{jobs_url}/SearchJobs/?jobOffset=12&jobRecordsPerPage=12": RenderedPage(
            html=f'<a href="{jobs_url}/JobDetail/Analyst/3">Analyst</a>',
            url=f"{jobs_url}/SearchJobs/?jobOffset=12&jobRecordsPerPage=12",
        ),
        f"{jobs_url}/JobDetail/Engineer/1": RenderedPage(
            html=_detail_page("Build the scraper."), url=f"{jobs_url}/JobDetail/Engineer/1",
        ),
    })

    jobs = AvatureScraper("acme", cloud_browser=browser).fetch()

    assert [(j.ats_id, j.description) for j in jobs] == [
        ("1", "Build the scraper."), ("2", None), ("3", None),
    ]


def test_aws_waf_challenge_is_read_through_the_cloud_browser(
    monkeypatch: pytest.MonkeyPatch, httpx_mock,
) -> None:
    import ats_scrapers.scrapers.avature as av

    async def never(self, base: str, company: str) -> list:
        raise AssertionError("httpcloak cannot pass a JavaScript challenge")

    monkeypatch.setattr(av.AvatureScraper, "_fetch_via_httpcloak", never)
    httpx_mock.add_response(
        url=_url("acme", 0),
        status_code=202,
        headers={"x-amzn-waf-action": "challenge"},
        text="<html><script>window.gokuProps = {};</script></html>",
    )
    jobs_url = "https://acme.avature.net/en_US/careers"
    browser = FakeBrowser({
        _url("acme", 0): RenderedPage(
            html=f'<a href="{jobs_url}/JobDetail/Engineer/1">Engineer</a>', url=_url("acme", 0),
        ),
    })

    jobs = AvatureScraper("acme", cloud_browser=browser).fetch()

    assert [j.ats_id for j in jobs] == ["1"]
    assert browser.requested[0] == _url("acme", 0)


def test_refused_board_whose_cloud_browser_fails_raises(refused_to_httpcloak) -> None:
    browser = FakeBrowser({})

    with pytest.raises(ScraperError, match=r"cloud browser failed: no page at"):
        AvatureScraper("acme", cloud_browser=browser).fetch()
    assert browser.requested == [_url("acme", 0)]


def test_cloud_browser_is_left_alone_when_httpx_reads_the_board(httpx_mock) -> None:
    httpx_mock.add_response(url=_url("acme", 0), text=ANCHOR_ONLY_PAGE)
    browser = FakeBrowser({})

    jobs = AvatureScraper("acme", cloud_browser=browser).fetch()

    assert len(jobs) == 2
    assert browser.requested == []


def test_get_description_falls_back_to_the_cloud_browser(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.scrapers.avature as av
    url = "https://acme.avature.net/careers/JobDetail/Engineer/1"
    httpx_mock.add_response(url=url, status_code=406)
    monkeypatch.setattr(av.AvatureScraper, "_http_get_via_httpcloak_sync", lambda self, u: None)
    browser = FakeBrowser({url: RenderedPage(html=_detail_page("Build the scraper."), url=url)})
    scraper = AvatureScraper("acme", cloud_browser=browser)
    [job] = scraper._parse_page(
        '<a href="/careers/JobDetail/Engineer/1">Engineer</a>', "https://acme.avature.net", "acme",
    )

    assert scraper.get_description(job) == "Build the scraper."


def test_cloud_browser_must_be_a_cloud_browser() -> None:
    with pytest.raises(TypeError, match="CloudBrowser"):
        AvatureScraper("acme", cloud_browser="wss://browser.example")  # type: ignore[arg-type]


def test_cloud_browser_passes_through_the_factories() -> None:
    browser = FakeBrowser({})

    by_ats = get_scraper("avature", "acme", cloud_browser=browser)
    by_url = get_scraper_for_url("https://acme.avature.net/careers", cloud_browser=browser)

    assert by_ats.cloud_browser is browser
    assert by_url.cloud_browser is browser


# --- proxy ------------------------------------------------------------------


def test_listing_goes_through_the_proxy_argument(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.scrapers.avature as av
    proxies: list[object] = []
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        proxies.append(kwargs.get("proxy"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(av.httpx, "AsyncClient", client)
    httpx_mock.add_response(url=_url("acme", 0), text=ANCHOR_ONLY_PAGE)

    AvatureScraper("acme", proxy="http://proxy.example:8080").fetch()

    assert proxies == ["http://proxy.example:8080"]


def test_httpcloak_goes_through_the_proxy_argument(monkeypatch) -> None:
    calls: list[dict] = []

    def get(url: str, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, text=ANCHOR_ONLY_PAGE, url=url)

    class Session:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        def get(self, url: str, **kwargs):
            return SimpleNamespace(status_code=200, text=ANCHOR_ONLY_PAGE, url=url)

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules, "httpcloak", SimpleNamespace(get=get, post=get, Session=Session),
    )
    scraper = AvatureScraper("acme", proxy="http://proxy.example:8080")

    scraper._fetch_page_via_httpcloak_sync("https://acme.avature.net", 0)
    scraper._http_get_via_httpcloak_sync("https://acme.avature.net/careers/JobDetail/A/1")
    scraper._post_json_via_httpcloak_sync("https://acme.avature.net/careers/_api", {})

    assert [c.get("proxy") for c in calls] == ["http://proxy.example:8080"] * 3


def test_failed_lookup_through_a_proxy_is_not_not_found(monkeypatch, httpx_mock) -> None:
    _dns(monkeypatch)
    httpx_mock.add_exception(_dns_error(socket.EAI_NONAME), url=_url("acme", 0), is_reusable=True)
    with pytest.raises(ScraperError) as excinfo:
        AvatureScraper("acme", proxy="http://proxy.invalid:8080").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_host_without_an_address_behind_a_proxy_is_not_found(monkeypatch, httpx_mock) -> None:
    asked = _dns(monkeypatch, missing=("gone.avature.net",))
    httpx_mock.add_exception(
        httpx.ProxyError("500 Internal Server Error"), url=_url("gone", 0), is_reusable=True,
    )
    with pytest.raises(CompanyNotFoundError, match="does not resolve"):
        AvatureScraper("gone", proxy="http://proxy.example:8080").fetch()
    assert asked == ["gone.avature.net", "avature.net"]


@pytest.mark.parametrize(
    ("missing", "errno"),
    [((), socket.EAI_NONAME), (("*",), socket.EAI_NONAME), (("*",), socket.EAI_AGAIN)],
    ids=["host-resolves", "resolver-answers-nothing", "resolver-down"],
)
def test_proxy_failure_without_proof_the_host_is_gone_is_not_not_found(
    monkeypatch, httpx_mock, missing, errno,
) -> None:
    _dns(monkeypatch, missing=missing, errno=errno)
    httpx_mock.add_exception(
        httpx.ProxyError("500 Internal Server Error"), url=_url("acme", 0), is_reusable=True,
    )
    with pytest.raises(ScraperError, match="500 Internal Server Error") as excinfo:
        AvatureScraper("acme", proxy="http://proxy.example:8080").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_retries_on_5xx_then_succeeds(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_url("acme", 0), status_code=503)
    httpx_mock.add_response(
        url=_url("acme", 0),
        text='<a href="/careers/JobDetail/A/1">A</a>',
    )
    jobs = AvatureScraper("acme").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_url("acme", 0), status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        AvatureScraper("acme").fetch()


# --- Offset cap ---------------------------------------------------------------


# The page an offset past the cap gets with a 500 (NVA, 2026-11-25).
AVATURE_ERROR_PAGE = (
    '<html><head><title> </title></head><body>'
    '<img src="/jscore/images/http/fatal.png" class="errorImage" alt="">'
    '<div class="title">Oops\u2026 Something went wrong</div>'
    '<div class="description">There was an error while processing your request. '
    "Please try again.</div></body></html>"
)


class CappedBoard:
    """A board that answers ``status`` from offset ``cap`` on and filters by
    the params its search form names. It ignores the values in ``ignored``.
    The first ``transient`` searches past the cap get a gateway's 503. A
    ``sortable`` board's form offers to sort it by posting date: a job's
    ``day``, else its place in ``jobs``. Jobs of one day keep their order
    both ways. One that ``ignores_order`` lists it the same way whatever the
    order asked."""

    def __init__(
        self,
        jobs: list[dict[str, str]],
        filters: dict[str, list[str]],
        *,
        cap: int,
        typed: tuple[str, ...] = (),
        ignored: tuple[str, ...] = (),
        status: int = 406,
        refusal: str | None = None,
        transient: int = 0,
        sortable: bool = False,
        ignores_order: bool = False,
    ) -> None:
        self.sortable = sortable
        self.ignores_order = ignores_order
        self.status = status
        self.refusal = refusal if refusal is not None else (
            AVATURE_ERROR_PAGE if status == 500 else "<title>Request not processed</title>"
        )
        self.transient = transient
        self.jobs = jobs
        self.filters = filters
        # Filters whose values the form loads from the values API.
        self.typed = typed
        self.ignored = ignored
        self.cap = cap
        self.searches: list[tuple[dict[str, str], int]] = []
        self.value_requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import ats_scrapers.scrapers.avature as av

        if request.url.path.endswith(av._OPTIONS_API):
            field_id = json.loads(request.content)["fieldId"]
            self.value_requests.append(field_id)
            values = self.filters[f"{field_id}[]"]
            return httpx.Response(200, json={
                "results": [{"id": int(v), "name": f"Value {v}"} for v in values],
                "more": False,
            })
        if "/JobDetail/" in request.url.path:
            return httpx.Response(404)
        params = dict(parse_qsl(request.url.query.decode()))
        offset = int(params.pop("jobOffset"))
        params.pop("jobRecordsPerPage", None)
        order = (params.pop("jobSort", None), params.pop("jobSortDirection", None))
        self.searches.append((params, offset))
        if offset >= self.cap and self.transient:
            self.transient -= 1
            return httpx.Response(503, text="<h1>503 Service Temporarily Unavailable</h1>")
        if offset >= self.cap and self.status != 200:
            return httpx.Response(self.status, text=self.refusal)
        applied = {
            k: v for k, v in params.items()
            if v in self.filters.get(k, ()) and v not in self.ignored
        }
        rows = [j for j in self.jobs if all(j.get(k) == v for k, v in applied.items())]
        def day(row: dict[str, str]) -> int:
            return int(row.get("day", self.jobs.index(row)))

        if not self.sortable or self.ignores_order:
            pass
        elif order == ("postedDate", "ASC"):
            rows.sort(key=day)
        elif order == ("postedDate", "DESC"):
            rows.sort(key=lambda row: -day(row))
        else:
            # Newest first by default, ties broken another way.
            rows = rows[::2][::-1] + rows[1::2]
        return httpx.Response(200, text=self.page(rows[offset:offset + 12]))

    def page(self, rows: list[dict[str, str]]) -> str:
        selects = "".join(
            f'<select name="{name}"><option value="">Select an option</option>'
            + "".join(
                f'<option value="{v}">{v}</option>' for v in values if name not in self.typed
            )
            + "</select>"
            for name, values in self.filters.items()
        )
        registrars = [{"wizardId": 18, "fields": [
            {"fieldId": name.removesuffix("[]"), "registrars": [{
                "type": "ValuesDatasource",
                "name": "DatasetSingleColumnFieldOptionsDatasource",
                "settings": {"portalUrlPath": "/careers", "datasetSchemaFieldId": "x"},
            }]}
            for name in self.typed
        ]}]
        anchors = "".join(
            f'<a href="/careers/JobDetail/Job/{j["id"]}">Job {j["id"]}</a>' for j in rows
        )
        return (
            '<html><head><meta name="avature.wizard.registrars" '
            f'content="{html.escape(json.dumps(registrars))}"></head><body>'
            '<form action="https://acme.avature.net/careers/SearchJobs" role="search">'
            + ('<input type="hidden" name="jobSort" value="">' if self.sortable else "")
            + f"{selects}</form>{anchors}</body></html>"
        )


@pytest.fixture
def offset_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    import ats_scrapers.scrapers.avature as av

    monkeypatch.setattr(av, "OFFSET_CAP", 24)
    return 24


def _ids(jobs) -> set[str]:
    return {j.ats_id for j in jobs}


@pytest.mark.parametrize("status", [406, 500])
def test_board_past_the_offset_cap_is_read_one_filter_value_at_a_time(
    httpx_mock, offset_cap, status, monkeypatch, caplog,
) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    jobs = [{"id": str(i), "732": "A" if i % 2 else "B"} for i in range(40)]
    board = CappedBoard(jobs, {"732": ["A", "B"]}, cap=offset_cap, status=status)
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.INFO):
        read = AvatureScraper("acme").fetch()

    assert "refuses offsets past 24 after 24 jobs" in caplog.text
    # A 406 is the cap at once; the error page is the cap once retries get it too.
    assert board.searches.count(({}, 24)) == (1 if status == 406 else 3)

    assert len(read) == 40
    assert _ids(read) == {str(i) for i in range(40)}
    assert ({}, 24) in board.searches
    assert sorted((p["732"], offset) for p, offset in board.searches if p) == [
        ("A", 0), ("A", 12), ("B", 0), ("B", 12),
    ]


def test_temporary_failure_past_the_cap_is_retried_not_taken_for_the_cap(
    httpx_mock, offset_cap, monkeypatch, caplog,
) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    jobs = [{"id": str(i), "732": "A" if i % 2 else "B"} for i in range(40)]
    board = CappedBoard(jobs, {"732": ["A", "B"]}, cap=offset_cap, status=200, transient=2)
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.INFO):
        read = AvatureScraper("acme").fetch()

    assert _ids(read) == {str(i) for i in range(40)}
    assert "refuses offsets past" not in caplog.text
    assert not [p for p, _ in board.searches if p]


@pytest.mark.parametrize(
    ("status", "refusal"),
    [
        (503, "<h1>503 Service Temporarily Unavailable</h1>"),
        (502, AVATURE_ERROR_PAGE),
        (500, "<h1>500 Internal Server Error</h1>"),
    ],
    ids=["gateway-503", "502-with-the-error-page", "500-from-elsewhere"],
)
def test_lasting_failure_past_the_cap_fails_the_board(
    httpx_mock, offset_cap, monkeypatch, status, refusal,
) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    jobs = [{"id": str(i), "732": "A" if i % 2 else "B"} for i in range(40)]
    board = CappedBoard(
        jobs, {"732": ["A", "B"]}, cap=offset_cap, status=status, refusal=refusal,
    )
    httpx_mock.add_callback(board, is_reusable=True)

    with pytest.raises(ScraperError, match=f"returned {status} at offset=24 after 3 retries"):
        AvatureScraper("acme").fetch()
    assert board.searches.count(({}, 24)) == 3
    assert not [p for p, _ in board.searches if p]


def test_slice_still_at_the_cap_is_split_by_values_from_the_values_api(
    httpx_mock, offset_cap,
) -> None:
    jobs = [
        {"id": str(i), "732": "A" if i % 3 else "B", "731[]": "6040" if i % 2 else "6041"}
        for i in range(60)
    ]
    board = CappedBoard(
        jobs, {"732": ["A", "B"], "731[]": ["6040", "6041"]}, cap=offset_cap, typed=("731[]",),
    )
    httpx_mock.add_callback(board, is_reusable=True)

    read = AvatureScraper("acme").fetch()

    assert _ids(read) == {str(i) for i in range(60)}
    assert board.value_requests == ["731"]
    assert ({"732": "A"}, 24) in board.searches
    assert ({"732": "A", "731[]": "6040"}, 0) in board.searches


def test_filter_value_the_search_ignores_is_not_walked_to_the_cap(
    httpx_mock, monkeypatch,
) -> None:
    import ats_scrapers.scrapers.avature as av

    monkeypatch.setattr(av, "OFFSET_CAP", 36)
    countries = {3: "37", 7: "39"}
    jobs = [
        {"id": str(i), "953[]": countries.get(i % 10, "38"), "732": "ABCD"[i % 4]}
        for i in range(60)
    ]
    board = CappedBoard(
        jobs,
        {"953[]": ["37", "38", "39"], "732": ["A", "B", "C", "D"]},
        cap=36,
        ignored=("38",),
    )
    httpx_mock.add_callback(board, is_reusable=True)

    read = AvatureScraper("acme").fetch()

    assert _ids(read) == {str(i) for i in range(60)}
    # Its first two pages and the board's last page before the cap are the
    # board's: the walk stops there.
    assert [s for s in board.searches if s[0] == {"953[]": "38"}] == [
        ({"953[]": "38"}, 0), ({"953[]": "38"}, 12), ({"953[]": "38"}, 24),
    ]
    # The other countries leave most jobs out: the company filter is read too.
    assert ({"732": "D"}, 0) in board.searches


def test_two_value_filters_are_tried_last(httpx_mock, offset_cap) -> None:
    jobs = [{"id": str(i), "730": "37" if i % 2 else "38", "732": "ABC"[i % 3]} for i in range(40)]
    board = CappedBoard(jobs, {"730": ["37", "38"], "732": ["A", "B", "C"]}, cap=offset_cap)
    httpx_mock.add_callback(board, is_reusable=True)

    read = AvatureScraper("acme").fetch()

    assert len(read) == 40
    assert not [s for s in board.searches if "730" in s[0]]


def test_capped_board_without_filters_keeps_what_it_read_and_warns(
    httpx_mock, offset_cap, caplog,
) -> None:
    board = CappedBoard([{"id": str(i)} for i in range(40)], {}, cap=offset_cap)
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.WARNING):
        read = AvatureScraper("acme").fetch()

    assert len(read) == 24
    assert "no filter splits it" in caplog.text


def test_capped_search_no_filter_splits_is_read_oldest_and_newest_first(
    httpx_mock, offset_cap, caplog,
) -> None:
    board = CappedBoard([{"id": str(i)} for i in range(40)], {}, cap=offset_cap, sortable=True)
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.WARNING):
        read = AvatureScraper("acme", include_descriptions=False).fetch()

    assert _ids(read) == {str(i) for i in range(40)}
    assert "no filter splits it" not in caplog.text


def test_capped_search_past_twice_the_cap_still_warns(httpx_mock, offset_cap, caplog) -> None:
    board = CappedBoard([{"id": str(i)} for i in range(60)], {}, cap=offset_cap, sortable=True)
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.WARNING):
        read = AvatureScraper("acme", include_descriptions=False).fetch()

    assert {str(i) for i in range(24)} | {str(i) for i in range(36, 60)} <= _ids(read)
    assert "no filter splits it" in caplog.text


def test_jobs_of_one_day_across_the_cap_are_read_to_the_end(httpx_mock, offset_cap, caplog) -> None:
    jobs = [{"id": str(i), "day": "0" if i < 12 else "1"} for i in range(32)]
    board = CappedBoard(jobs, {}, cap=offset_cap, sortable=True)
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.WARNING):
        read = AvatureScraper("acme", include_descriptions=False).fetch()

    assert _ids(read) == {str(i) for i in range(32)}
    assert "no filter splits it" not in caplog.text


def test_walks_that_meet_inside_one_day_are_not_taken_for_read(
    httpx_mock, offset_cap, caplog,
) -> None:
    jobs = [{"id": str(i), "day": str(min(i // 6, 1) + (i >= 30))} for i in range(36)]
    board = CappedBoard(jobs, {}, cap=offset_cap, sortable=True)
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.WARNING):
        AvatureScraper("acme", include_descriptions=False).fetch()

    assert "no filter splits it" in caplog.text


def test_oldest_first_walk_cut_short_is_not_taken_for_read(
    refused_to_httpcloak, offset_cap, caplog,
) -> None:
    board = CappedBoard([{"id": str(i)} for i in range(40)], {}, cap=offset_cap, sortable=True)

    class BoardBrowser(CloudBrowser):
        async def render(self, url: str) -> RenderedPage:
            query = dict(parse_qsl(url.split("?", 1)[1]))
            if query.get("jobSortDirection") == "ASC" and query["jobOffset"] != "0":
                raise ScraperError("browser: the page did not load")
            response = board(httpx.Request("GET", url))
            if response.status_code >= 400:
                raise ScraperError(f"browser: {url} answered {response.status_code}")
            return RenderedPage(html=response.text, url=url)

    with caplog.at_level(logging.WARNING):
        AvatureScraper("acme", cloud_browser=BoardBrowser(), include_descriptions=False).fetch()

    assert "no filter splits it" in caplog.text


def test_search_that_ignores_the_order_is_not_taken_for_read(
    httpx_mock, offset_cap, caplog,
) -> None:
    board = CappedBoard(
        [{"id": str(i)} for i in range(40)], {}, cap=offset_cap, sortable=True, ignores_order=True,
    )
    httpx_mock.add_callback(board, is_reusable=True)

    with caplog.at_level(logging.WARNING):
        read = AvatureScraper("acme", include_descriptions=False).fetch()

    assert len(read) == 24
    assert "no filter splits it" in caplog.text


def test_board_under_the_offset_cap_is_read_in_one_pass(httpx_mock, offset_cap) -> None:
    jobs = [{"id": str(i), "731[]": "6040"} for i in range(20)]
    board = CappedBoard(jobs, {"731[]": ["6040", "6041"]}, cap=offset_cap, typed=("731[]",))
    httpx_mock.add_callback(board, is_reusable=True)

    read = AvatureScraper("acme").fetch()

    assert len(read) == 20
    assert board.searches == [({}, 0), ({}, 12)]
    assert board.value_requests == []


def _rows(ids: range) -> str:
    return "".join(f'<a href="/careers/JobDetail/Job/{i}">Job {i}</a>' for i in ids)


def test_406_below_the_offset_cap_is_still_a_refusal(httpx_mock) -> None:
    httpx_mock.add_response(url=_url("acme", 0), text=_rows(range(12)))
    httpx_mock.add_response(url=_url("acme", 12), status_code=406)

    with pytest.raises(ScraperError, match=r"406 at offset=12"):
        AvatureScraper("acme").fetch()


def test_page_refused_partway_is_read_once_the_rate_limit_lets_it_through(
    httpx_mock, monkeypatch,
) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    monkeypatch.setattr(av, "RATE_LIMIT_WAITS", (60.0, 240.0))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(av.asyncio, "sleep", sleep)
    httpx_mock.add_response(url=_url("acme", 0), text=_rows(range(12)))
    httpx_mock.add_response(url=_url("acme", 12), status_code=406)
    httpx_mock.add_response(url=_url("acme", 12), status_code=406)
    httpx_mock.add_response(url=_url("acme", 12), text=_rows(range(12, 20)))

    read = AvatureScraper("acme").fetch()

    assert _ids(read) == {str(i) for i in range(20)}
    assert waits == [60.0, 240.0]


def test_slice_refused_at_its_start_waits_instead_of_changing_client(
    httpx_mock, offset_cap, monkeypatch,
) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)

    async def no_httpcloak(self, base: str, company: str) -> list:
        raise AssertionError("the board was read again through httpcloak")

    monkeypatch.setattr(av.AvatureScraper, "_fetch_via_httpcloak", no_httpcloak)
    jobs = [{"id": str(i), "732": "A" if i % 2 else "B"} for i in range(40)]
    board = CappedBoard(jobs, {"732": ["A", "B"]}, cap=offset_cap)
    refused: list[str] = []

    def rate_limited(request: httpx.Request) -> httpx.Response:
        if "732=A" in str(request.url) and not refused:
            refused.append(str(request.url))
            return httpx.Response(406)
        return board(request)

    httpx_mock.add_callback(rate_limited, is_reusable=True)

    read = AvatureScraper("acme").fetch()

    assert _ids(read) == {str(i) for i in range(40)}
    assert refused


class RecordingPacer:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def wait(self, url: str, *, timeout: float | None = None) -> None:
        self.urls.append(url)

    def wait_sync(self, url: str, *, timeout: float | None = None) -> None:
        self.urls.append(url)


def test_pacer_spaces_listing_values_and_job_requests(httpx_mock, offset_cap) -> None:
    import ats_scrapers.scrapers.avature as av
    jobs = [{"id": str(i), "731[]": "6040" if i % 2 else "6041"} for i in range(30)]
    board = CappedBoard(jobs, {"731[]": ["6040", "6041"]}, cap=offset_cap, typed=("731[]",))
    httpx_mock.add_callback(board, is_reusable=True)
    pacer = RecordingPacer()

    read = AvatureScraper("acme", pacer=pacer).fetch()

    assert len(read) == 30
    sent = [str(request.url) for request in httpx_mock.get_requests()]
    assert sorted(pacer.urls) == sorted(sent)
    assert any(url.endswith(av._OPTIONS_API) for url in pacer.urls)
    assert sum("/JobDetail/" in url for url in pacer.urls) == 30


def test_pacer_spaces_httpcloak_requests(monkeypatch) -> None:
    def get(url: str, **kwargs):
        return SimpleNamespace(status_code=200, text=ANCHOR_ONLY_PAGE, url=url)

    monkeypatch.setitem(sys.modules, "httpcloak", SimpleNamespace(get=get, post=get))
    pacer = RecordingPacer()
    scraper = AvatureScraper("acme", pacer=pacer)

    scraper._fetch_page_via_httpcloak_sync("https://acme.avature.net", 0)
    scraper._http_get_via_httpcloak_sync("https://acme.avature.net/careers/JobDetail/A/1")
    scraper._post_json_via_httpcloak_sync("https://acme.avature.net/careers/_api", {})

    assert pacer.urls == [
        "https://acme.avature.net/careers/SearchJobs/",
        "https://acme.avature.net/careers/JobDetail/A/1",
        "https://acme.avature.net/careers/_api",
    ]


def _httpcloak_answers(monkeypatch, *answers: tuple[int, str]) -> list[str]:
    """Stands in for httpcloak: one answer per request, the last one repeated."""
    queue = list(answers)
    sent: list[str] = []

    def get(url: str, **kwargs):
        sent.append(url)
        status, text = queue.pop(0) if len(queue) > 1 else queue[0]
        return SimpleNamespace(status_code=status, text=text, url=url)

    monkeypatch.setitem(sys.modules, "httpcloak", SimpleNamespace(get=get, post=get))
    return sent


def test_httpcloak_takes_the_error_page_past_the_cap_for_the_cap(monkeypatch) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    sent = _httpcloak_answers(monkeypatch, (500, AVATURE_ERROR_PAGE))
    scraper = AvatureScraper("acme")

    with pytest.raises(av._OffsetCapError):
        scraper._fetch_page_via_httpcloak_sync("https://acme.avature.net", av.OFFSET_CAP + 10)
    assert len(sent) == 3
    with pytest.raises(ScraperError, match="returned 500 at offset=12"):
        scraper._fetch_page_via_httpcloak_sync("https://acme.avature.net", 12)


@pytest.mark.parametrize(
    ("status", "text"),
    [(503, "<h1>Service Unavailable</h1>"), (502, AVATURE_ERROR_PAGE), (500, "")],
)
def test_httpcloak_lasting_other_failure_past_the_cap_fails(monkeypatch, status, text) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    sent = _httpcloak_answers(monkeypatch, (status, text))

    with pytest.raises(ScraperError, match=f"httpcloak returned {status}") as excinfo:
        AvatureScraper("acme")._fetch_page_via_httpcloak_sync(
            "https://acme.avature.net", av.OFFSET_CAP + 10,
        )
    assert not isinstance(excinfo.value, av._OffsetCapError)
    assert len(sent) == 3


def test_httpcloak_retries_a_temporary_failure_past_the_cap(monkeypatch) -> None:
    import ats_scrapers.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    sent = _httpcloak_answers(
        monkeypatch, (503, "<h1>Service Unavailable</h1>"), (200, "<html>page</html>"),
    )

    text, _ = AvatureScraper("acme")._fetch_page_via_httpcloak_sync(
        "https://acme.avature.net", av.OFFSET_CAP + 10,
    )
    assert text == "<html>page</html>"
    assert len(sent) == 2


@pytest.mark.parametrize("raises", [True, False], ids=["raises-on-406", "renders-406"])
def test_browser_reads_a_capped_board_by_the_filters_its_form_lists(
    refused_to_httpcloak, offset_cap, raises: bool,
) -> None:
    jobs = [{"id": str(i), "732": "A" if i % 2 else "B"} for i in range(40)]
    board = CappedBoard(jobs, {"732": ["A", "B"]}, cap=offset_cap)

    class BoardBrowser(CloudBrowser):
        async def render(self, url: str) -> RenderedPage:
            response = board(httpx.Request("GET", url))
            if raises and response.status_code >= 400:
                raise ScraperError(f"browser: {url} answered {response.status_code}")
            return RenderedPage(html=response.text, url=url)

    read = AvatureScraper("acme", cloud_browser=BoardBrowser()).fetch()

    assert _ids(read) == {str(i) for i in range(40)}
    assert ({"732": "B"}, 12) in board.searches


def test_slice_that_starts_like_the_board_is_still_read(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.avature as av

    monkeypatch.setattr(av, "OFFSET_CAP", 36)
    # Company A holds the board's first 24 jobs, then jobs past the cap.
    jobs = [{"id": str(i), "732": "A" if i < 24 or 36 <= i < 48 else "B"} for i in range(64)]
    board = CappedBoard(jobs, {"732": ["A", "B"]}, cap=36)
    httpx_mock.add_callback(board, is_reusable=True)

    read = AvatureScraper("acme").fetch()

    assert _ids(read) == {str(i) for i in range(64)}


def test_filter_set_by_the_board_url_is_kept(httpx_mock, offset_cap) -> None:
    jobs = [
        {"id": str(i), "732": "A" if i < 40 else "B", "731[]": "6040" if i % 2 else "6041"}
        for i in range(60)
    ]
    board = CappedBoard(jobs, {"732": ["A", "B"], "731[]": ["6040", "6041"]}, cap=offset_cap)
    httpx_mock.add_callback(board, is_reusable=True)

    read = AvatureScraper("https://acme.avature.net/careers/SearchJobs/?732=A").fetch()

    assert _ids(read) == {str(i) for i in range(40)}
    assert all(params.get("732") == "A" for params, _ in board.searches)


# --- Job pages ----------------------------------------------------------------


def _field(label: str, value: str, classes: str = "") -> str:
    head = f'<div class="article__content__view__field__label">{label}</div>' if label else ""
    return (
        f'<div class="article__content__view__field {classes}">{head}'
        f'<div class="article__content__view__field__value">{value}</div></div>'
    )


def _block(*fields: str) -> str:
    return (
        '<article class="article article--details"><div class="article__content">'
        f'<div class="article__content__view">{"".join(fields)}</div></div></article>'
    )


FACTS = _block(
    _field("Job Name", "Receptionist"),
    _field("Ref #", "374"),
    _field("Date posted", "25-Nov-2026"),
    _field("Location", "Taipa Showroom"),
)
LONG = "Answer phone calls, receive visitors and handle the front desk. " * 3


def _read(page: str, url: str = "https://acme.avature.net/careers/JobDetail/A/1"):
    from ats_scrapers.scrapers.avature import _apply_detail_to_job, _parse_detail

    [job] = AvatureScraper("acme")._parse_page(
        f'<a href="{url}">Receptionist</a>', "https://acme.avature.net", "acme",
    )
    _apply_detail_to_job(job, _parse_detail(page, url))
    return job


def test_job_body_in_labelled_fields_is_read() -> None:
    job = _read(FACTS + _block(
        _field("Job Description", LONG),
        _field("Job Requirements", "Form 6<br>Two years of experience"),
        _field("Qualification", "Secondary school"),
    ))

    assert job.description == (
        f"Job Description\n{LONG.strip()}\n\n"
        "Job Requirements\nForm 6\nTwo years of experience\n\n"
        "Qualification\nSecondary school"
    )
    assert job.posted_at == datetime(2026, 11, 25)
    assert job.location == "Taipa Showroom"


def test_job_body_in_one_labelled_field_is_read() -> None:
    job = _read(FACTS + _block(_field("Position Description", f"<p>{LONG}</p>")))

    assert job.description == f"Position Description\n{LONG.strip()}"


def test_field_label_the_page_hides_is_left_out() -> None:
    job = _read(_block(_field("Description", f"<p>{LONG}</p>", "regular-field-label--hidden")))

    assert job.description == LONG.strip()


def test_facts_and_hidden_fields_stay_out_of_the_body() -> None:
    job = _read(
        _block(
            _field("Location", "Aurora Health Center"),
            _field("", "4448 W Loomis Rd"),
            _field("Compensation", "$300,000"),
        )
        + _block(
            _field("CareerArc Code", "CA-SB", "visibility--hidden"),
            _field("", "Hybrid: #LI-Hybrid", "visibility--hidden"),
            _field("", f"<div>{LONG}</div>"),
            _field("Min salary", "2,130,750", "visibility--hidden"),
            _field("Max salary", "3,551,250", "visibility--hidden"),
        )
    )

    assert job.description == LONG.strip()


def test_rows_pasted_inside_a_field_are_read_once() -> None:
    pasted = _field("Duties", "<ul><li>Design services</li></ul>")
    job = _read(FACTS + _block(_field("Responsibilities", f"<div>{pasted}</div>")))

    assert job.description == "Responsibilities\nDuties\nDesign services"


def test_blocks_wrapped_in_one_more_block_are_read_once() -> None:
    facts = _block(_field("Job Number:", "R0250531"), _field("Location:", "Schriever AFB,CO,US"))
    body = _block(_field("", f"<p>{LONG}</p>"))
    job = _read(f'<article class="article article--details">{facts}{body}</article>')

    assert job.description == LONG.strip()
    assert job.location == "Schriever AFB,CO,US"


def test_facts_above_the_body_in_its_block_stay_out_of_it() -> None:
    job = _read(_block(
        _field("Remote Work:", "No"),
        _field("Job Number:", "R0250531"),
        _field("", f"<p>{LONG}</p>"),
        _field("Clearance", "Secret"),
    ))

    assert job.description == f"{LONG.strip()}\n\nClearance\nSecret"


@pytest.mark.parametrize("section", ["jobDescription", "detailDescription"])
def test_other_description_sections_are_read(section: str) -> None:
    page = f'<h2>Salesforce Developer</h2><div class="{section}"><p>Build flows.</p></div>'

    assert _read(page).description == "Salesforce Developer\nBuild flows."


def test_older_career_site_template_is_read() -> None:
    page = """
    <div class="fieldSet"><p class="fieldSetLabel">Location:</p>
      <div class="fieldSetValue" itemprop="jobLocation"><div>Boulder, Colorado</div></div></div>
    <div class="fieldSet"><p class="fieldSetLabel">Date Posted:</p>
      <div class="fieldSetValue"> 24-Nov-2026 </div></div>
    <div class="fieldSet"><p class="fieldSetLabel">Posting Close Date:</p>
      <div class="fieldSetValue"> 05-Jan-2027 </div></div>
    <div class="js-accordion__panel"><h3>Job Summary</h3>
      <div itemprop="description" class="jobDetailDescription"><p>Build sets.</p></div></div>
    <div class="js-accordion__panel"><h3>Special Instructions</h3>
      <div itemprop="description" class="jobDetailDescription"><p>Send a resume.</p></div></div>
    """
    job = _read(page)

    assert job.description == "Job Summary\nBuild sets.\n\nSpecial Instructions\nSend a resume."
    assert job.location == "Boulder, Colorado"
    assert job.posted_at == datetime(2026, 11, 24)
    assert job.application_deadline == datetime(2027, 1, 5)


def test_info_sections_are_read() -> None:
    page = """
    <div class="moreDetails"><div class="infoSection">
      <div class="infoTitle">Key Tasks</div><div class="infoDes"><ul><li>Meet customers</li></ul></div>
    </div></div>
    """

    assert _read(page).description == "Key Tasks\nMeet customers"


def test_job_posting_data_fills_what_the_page_leaves_out() -> None:
    posting = {
        "@context": "https://schema.org/", "@type": "JobPosting",
        "description": "<p>Support the leadership team.</p>",
        "datePosted": "2026-11-20", "employmentType": "FULL_TIME",
        "jobLocation": {"@type": "Place", "address": {
            "addressLocality": "Helena", "addressRegion": "MT", "addressCountry": "USA",
        }},
    }
    page = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
    job = _read(page)

    assert job.description == "Support the leadership team."
    assert job.posted_at == datetime(2026, 11, 20)
    assert job.location == "Helena, MT, USA"
    assert job.employment_type == "FULL_TIME"


def test_place_given_in_parts_is_joined() -> None:
    job = _read(_block(_field("City:", "San Diego"), _field("State:", "California")))

    assert job.location == "San Diego, California"


def test_job_body_loaded_from_the_detail_widget_is_read(httpx_mock) -> None:
    detail = "https://acme.avature.net/careers/JobDetail/Engineer/1"
    widget = "https://acme.avature.net/careers/JobDetailWidget?jobId=1"
    httpx_mock.add_response(
        url=_url("acme", 0), text='<a href="/careers/JobDetail/Engineer/1">Engineer</a>',
    )
    httpx_mock.add_response(
        url=detail,
        text='<div id="jobDetailWidget"></div><script>var widgets = {"jobDetailWidget": '
        '{"jobDetailWidgetEnable": 1, "url": "https://acme.avature.net/careers/'
        'JobDetailWidget?jobId=1", "selector": "#jobDetailWidget"}};</script>',
    )
    httpx_mock.add_response(url=widget, text=FACTS + _block(_field("", f"<p>{LONG}</p>")))

    [job] = AvatureScraper("acme").fetch()

    assert job.description == LONG.strip()
    assert job.location == "Taipa Showroom"


def test_board_listed_on_an_all_jobs_page_is_read(httpx_mock) -> None:
    base = "https://careers.example.org/careersmarketplace/AllJobs"
    httpx_mock.add_response(
        url=f"{base}/?jobOffset=0&jobRecordsPerPage=12",
        text='<a href="https://careers.example.org/careersmarketplace/PipelineDetail/Nurse/7">Nurse</a>',
    )

    [job] = AvatureScraper(base, include_descriptions=False).fetch()

    assert job.ats_id == "7"


def test_board_read_without_descriptions_requests_no_job_page(httpx_mock) -> None:
    httpx_mock.add_response(url=_url("acme", 0), text=ANCHOR_ONLY_PAGE)

    jobs = AvatureScraper("acme", include_descriptions=False).fetch()

    assert len(jobs) == 2
    assert [str(r.url) for r in httpx_mock.get_requests()] == [_url("acme", 0)]


def test_get_description_fills_the_job_from_its_page(httpx_mock) -> None:
    url = "https://acme.avature.net/careers/JobDetail/Engineer/1"
    httpx_mock.add_response(url=url, text=FACTS + _block(_field("", f"<p>{LONG}</p>")))
    scraper = AvatureScraper("acme")
    [job] = scraper._parse_page(
        f'<a href="{url}">Engineer</a>', "https://acme.avature.net", "acme",
    )

    assert scraper.get_description(job) == LONG.strip()
    assert job.description == LONG.strip()
    assert job.posted_at == datetime(2026, 11, 25)
    assert job.location == "Taipa Showroom"


def test_get_description_reads_the_way_the_board_was_read(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.scrapers.avature as av
    url = "https://acme.avature.net/careers/JobDetail/Engineer/1"
    read: list[str] = []

    def cloak(self, u: str):
        read.append(u)
        return SimpleNamespace(status_code=200, text=_block(_field("", f"<p>{LONG}</p>")))

    monkeypatch.setattr(av.AvatureScraper, "_http_get_via_httpcloak_sync", cloak)
    scraper = AvatureScraper("acme")
    scraper._reader = "httpcloak"
    [job] = scraper._parse_page(
        f'<a href="{url}">Engineer</a>', "https://acme.avature.net", "acme",
    )

    assert scraper.get_description(job) == LONG.strip()
    assert read == [url]
    assert httpx_mock.get_requests() == []


def test_get_description_tries_the_next_client_when_a_page_gives_no_body(
    monkeypatch, httpx_mock,
) -> None:
    import ats_scrapers.scrapers.avature as av
    url = "https://acme.avature.net/careers/JobDetail/Engineer/1"
    widget = "https://acme.avature.net/careers/JobDetailWidget?jobId=1"
    httpx_mock.add_response(
        url=url, text=f'<script>var w = {{"jobDetailWidget": {{"url": "{widget}"}}}};</script>',
    )
    httpx_mock.add_response(url=widget, status_code=406)
    monkeypatch.setattr(
        av.AvatureScraper, "_http_get_via_httpcloak_sync",
        lambda self, u: SimpleNamespace(status_code=200, text=_block(_field("", f"<p>{LONG}</p>"))),
    )
    scraper = AvatureScraper("acme")
    scraper._reader = "httpx"
    [job] = scraper._parse_page(
        f'<a href="{url}">Engineer</a>', "https://acme.avature.net", "acme",
    )

    assert scraper.get_description(job) == LONG.strip()


def test_board_read_over_http_does_not_send_its_job_pages_to_the_browser(
    monkeypatch, httpx_mock,
) -> None:
    import ats_scrapers.scrapers.avature as av
    url = "https://acme.avature.net/careers/JobDetail/Engineer/1"
    httpx_mock.add_response(url=url, status_code=404)
    monkeypatch.setattr(av.AvatureScraper, "_http_get_via_httpcloak_sync", lambda self, u: None)
    browser = FakeBrowser({url: RenderedPage(html=_detail_page("Build the scraper."), url=url)})
    scraper = AvatureScraper("acme", cloud_browser=browser)
    scraper._reader = "httpx"
    [job] = scraper._parse_page(
        f'<a href="{url}">Engineer</a>', "https://acme.avature.net", "acme",
    )

    assert scraper.get_description(job) is None
    assert browser.requested == []
