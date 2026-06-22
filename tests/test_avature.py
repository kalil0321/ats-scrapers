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

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import AvatureScraper, ScraperRegistry, get_scraper
from jobhive.scrapers.avature import _paginated_search_url


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.avature as av
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


def test_browserbase_pagination_url_preserves_searchjobs_maps_query_params() -> None:
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
    import jobhive.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_url("missing", 0), status_code=404)
    with pytest.raises(CompanyNotFoundError):
        AvatureScraper("missing").fetch()


def test_retries_on_5xx_then_succeeds(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_url("acme", 0), status_code=503)
    httpx_mock.add_response(
        url=_url("acme", 0),
        text='<a href="/careers/JobDetail/A/1">A</a>',
    )
    jobs = AvatureScraper("acme").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.avature as av
    monkeypatch.setattr(av, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_url("acme", 0), status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        AvatureScraper("acme").fetch()
