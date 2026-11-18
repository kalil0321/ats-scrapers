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

import socket
import sys
from types import SimpleNamespace

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


def test_failed_lookup_behind_an_environment_proxy_is_not_not_found(
    monkeypatch, httpx_mock,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
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
    with pytest.raises(ScraperError, match="AWS WAF"):
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

    monkeypatch.setitem(sys.modules, "httpcloak", SimpleNamespace(get=get))
    scraper = AvatureScraper("acme", proxy="http://proxy.example:8080")

    scraper._fetch_page_via_httpcloak_sync("https://acme.avature.net", 0)
    scraper._http_get_via_httpcloak_sync("https://acme.avature.net/careers/JobDetail/A/1")

    assert [c.get("proxy") for c in calls] == ["http://proxy.example:8080"] * 2


def test_failed_lookup_through_a_proxy_is_not_not_found(httpx_mock) -> None:
    httpx_mock.add_exception(_dns_error(socket.EAI_NONAME), url=_url("acme", 0), is_reusable=True)
    with pytest.raises(ScraperError) as excinfo:
        AvatureScraper("acme", proxy="http://proxy.invalid:8080").fetch()
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
