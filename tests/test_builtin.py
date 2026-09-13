"""Built In pagination, transport and source-quality contracts."""

import json

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import BuiltInScraper, ScraperRegistry


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def skip(seconds):
        return None
    monkeypatch.setattr("ats_scrapers.scrapers.builtin.asyncio.sleep", skip)


def listing(page=1, *, last=2, job_id=None, company="Acme", description="Summary."):
    job_id = str(job_id or page)
    payload = {"@graph": [{"@type": "ItemList", "itemListElement": [{
        "url": f"https://builtin.com/job/engineer/{job_id}",
        "name": "Engineer", "description": description,
    }]}]}
    next_link = (
        f'<a aria-label="Go to Next Page" href="/jobs?page={page + 1}">Next</a>'
        if page < last else ""
    )
    return (
        '<script type="application/ld&#x2B;json">' + json.dumps(payload) + '</script>'
        '<div data-id="job-card"><a data-id="job-card-title" '
        f'data-builtin-track-job-id="{job_id}" href="/job/engineer/{job_id}">Engineer</a>'
        f'<a data-id="company-title">{company}</a>'
        '<span aria-label="Job locations" data-bs-title="&lt;div&gt;Paris, France&lt;/div&gt;'
        '&lt;div&gt;Berlin, Germany&lt;/div&gt;">2 Locations</span>'
        '<div><div><i class="fa-house-building"></i></div><span>Hybrid</span></div></div>'
        f'<ul class="pagination"><a class="disabled">{page}</a>{next_link}</ul>'
    )


def scraper(**kwargs):
    return BuiltInScraper("builtin", request_delay=0, include_descriptions=False, **kwargs)


def test_registry():
    assert ScraperRegistry.get(ATSType.BUILTIN) is BuiltInScraper


def test_reads_all_pages_and_employer_fields(httpx_mock):
    for page in (1, 2):
        httpx_mock.add_response(url=f"https://builtin.com/jobs?page={page}", text=listing(page))
    jobs = scraper().fetch()
    assert [job.ats_id for job in jobs] == ["1", "2"]
    assert all(job.company == "Acme" for job in jobs)
    assert jobs[0].location == "Paris, France; Berlin, Germany"
    assert jobs[0].is_remote is None
    assert jobs[0].raw == {"description_kind": "summary", "workplace_type": "Hybrid"}


@pytest.mark.parametrize("encoded", [True, False])
def test_json_ld_types_and_html_descriptions(encoded):
    text = listing(last=1, description="<p>Build <b>things</b>&nbsp;today.</p>")
    if not encoded:
        text = text.replace("ld&#x2B;json", "ld+json")
    job = scraper()._parse_listing(text)[0]
    assert job.description == "<p>Build <b>things</b>\u00a0today.</p>"


@pytest.mark.parametrize("status", [403, 429, 500])
def test_failure_after_first_page_is_not_partial_success(httpx_mock, monkeypatch, status):
    monkeypatch.setattr(BuiltInScraper, "fetch_escalate", False)
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=listing())
    httpx_mock.add_response(url="https://builtin.com/jobs?page=2", status_code=status, is_reusable=True)
    with pytest.raises(ScraperError):
        scraper().fetch()


def test_rate_limit_retry_after_is_respected(httpx_mock, monkeypatch):
    delays = []
    async def record(seconds):
        delays.append(seconds)
    monkeypatch.setattr("ats_scrapers.fetch.asyncio.sleep", record)
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", status_code=429,
                            headers={"Retry-After": "12"})
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=listing(last=1))
    assert len(scraper().fetch()) == 1
    assert delays == [12.0]


def test_page_cap_is_failure(httpx_mock):
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=listing())
    with pytest.raises(ScraperError, match="max_pages"):
        scraper(max_pages=1).fetch()


def test_repeated_page_is_failure(httpx_mock):
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=listing())
    httpx_mock.add_response(url="https://builtin.com/jobs?page=2", text=listing(2, job_id=1))
    with pytest.raises(ScraperError, match="repeated"):
        scraper().fetch()


def test_missing_employer_is_failure(httpx_mock):
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=listing(last=1, company=""))
    with pytest.raises(ScraperError, match="employer"):
        scraper().fetch()


@pytest.mark.parametrize("page", ["<html>challenge</html>", '<script type="application/ld+json">{}</script>'])
def test_unrecognized_listing_is_failure(page):
    with pytest.raises(ScraperError):
        scraper()._parse_listing(page)


@pytest.mark.parametrize("change", [
    lambda text: text.replace("/jobs?page=2", "https://evil.example/jobs?page=2"),
    lambda text: text.replace("/jobs?page=2", "/jobs?page=1"),
    lambda text: text.replace('class="disabled">1', 'class="disabled">9'),
    lambda text: text.replace('aria-label="Go to Next Page"', '').replace('>Next</a>', '>2</a>'),
])
def test_invalid_pagination_is_failure(change):
    with pytest.raises(ScraperError):
        scraper()._next_page(change(listing()), 1)


def detail(identifier="1", description="<p>Full responsibilities.</p>"):
    return '<script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "identifier": {"value": identifier},
        "description": description,
    }) + '</script>'


def test_full_descriptions_replace_summaries(httpx_mock):
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=listing(last=1))
    httpx_mock.add_response(url="https://builtin.com/job/engineer/1", text=detail())
    job = BuiltInScraper("builtin", request_delay=0).fetch()[0]
    assert job.description == "<p>Full responsibilities.</p>"
    assert job.raw["description_kind"] == "full"


def test_public_url_alias_matches_card_and_detail_identity(httpx_mock):
    body = listing(last=1).replace('data-builtin-track-job-id="1"',
                                   'data-builtin-track-job-id="99"')
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=body)
    httpx_mock.add_response(url="https://builtin.com/job/engineer/1", text=detail("99"))
    job = BuiltInScraper("builtin", request_delay=0).fetch()[0]
    assert job.ats_id == "1"
    assert job.company == "Acme"
    assert job.location == "Paris, France; Berlin, Germany"
    assert job.raw["source_job_id"] == "99"
    assert job.description == "<p>Full responsibilities.</p>"


@pytest.mark.parametrize("body", [detail("2"), detail(description=""), "<html>Challenge</html>"])
def test_bad_details_are_not_full_descriptions(body):
    job = scraper()._parse_listing(listing(last=1))[0]
    with pytest.raises(ScraperError):
        scraper()._detail_description(body, job)


def test_no_paid_fallback_even_with_key(httpx_mock, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "unused")
    httpx_mock.add_response(url="https://builtin.com/jobs?page=1", text=listing(last=1))
    assert len(scraper().fetch()) == 1


def test_pipeline_fail_closed():
    from scripts.run_pipeline import CONFIGS
    config = CONFIGS["builtin"]
    assert all(config[key] for key in (
        "fail_closed_on_any_error", "fail_closed_on_not_found", "fail_closed_on_empty",
    ))
