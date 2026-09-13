"""Credential-free Wellfound parsing and browser pagination contracts."""

import json
import sys
from types import SimpleNamespace

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.scrapers.wellfound import WellfoundScraper


def page_html(page=1, *, pages=2, total=2, job_id=None, ats_source=None, auto_posted=False):
    job_id = str(job_id or page)
    params = json.dumps({"page": page, "role": "engineer"}, separators=(",", ":"))
    data = {
        "ROOT_QUERY": {"talent": {f"seoLandingPageJobSearchResults({params})": {
            "pageCount": pages, "totalJobCount": total, "startups": [{"__ref": "StartupResult:1"}],
        }}},
        "StartupResult:1": {"name": "Acme", "slug": "acme",
                            "highlightedJobListings": [{"__ref": f"JobListingSearchResult:{job_id}"}]},
        f"JobListingSearchResult:{job_id}": {
            "id": job_id, "slug": "engineer", "title": "Engineer", "description": "Full duties.",
            "locationNames": ["Paris", "Berlin"], "remote": False,
            "liveStartAt": 1787861184, "compensation": "$100k – $150k", "jobType": "full-time",
            "autoPosted": auto_posted, "atsSource": ats_source,
        },
    }
    payload = {"props": {"pageProps": {"apolloState": {"data": data}}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'


class Browser:
    def __init__(self, pages, status=200):
        self.pages = iter(pages)
        self.status = status
        self.closed = False
        self.urls = []

    async def new_page(self):
        return self

    async def goto(self, url, **kwargs):
        self.urls.append(url)
        self.content_value = next(self.pages)
        return SimpleNamespace(status=self.status)

    async def wait_for_selector(self, *args, **kwargs):
        return None

    async def content(self):
        return self.content_value

    async def close(self):
        self.closed = True


def install_browser(monkeypatch, pages, status=200):
    browser = Browser(pages, status)
    async def launch(**kwargs):
        assert kwargs["proxy"] is None
        return browser
    monkeypatch.setitem(sys.modules, "cloakbrowser", SimpleNamespace(launch_async=launch))
    return browser


def scraper(**kwargs):
    return WellfoundScraper("wellfound", role_slugs=("engineer",), request_delay=0, **kwargs)


def test_paginates_without_paid_service(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "must-not-be-used")
    browser = install_browser(monkeypatch, [page_html(1), page_html(2)])
    jobs = scraper().fetch()
    assert [job.ats_id for job in jobs] == ["1", "2"]
    assert jobs[0].company == "Acme"
    assert jobs[0].description == "Full duties."
    assert jobs[0].location == "Paris, Berlin"
    assert jobs[0].salary_min == 100000
    assert jobs[0].salary_max == 150000
    assert jobs[0].posted_at.timestamp() == 1787861184
    assert len(browser.urls) == 2
    assert browser.closed


@pytest.mark.parametrize("ats_source,auto_posted", [("AtsIntegration::Greenhouse::Listing", False), (None, True)])
def test_excludes_ats_imports_and_automated_posts(ats_source, auto_posted):
    jobs, identifiers, pages, total = scraper()._parse_browser_page(
        page_html(pages=1, total=1, ats_source=ats_source, auto_posted=auto_posted), "engineer", 1,
    )
    assert jobs == []
    assert identifiers == {"1"}
    assert pages == total == 1


@pytest.mark.parametrize("text", ["<html>Security check</html>", page_html(2), page_html(pages=0),
                                  page_html().replace('"Full duties."', 'null'),
                                  page_html().replace('"Acme"', '""')])
def test_malformed_or_wrong_page_fails(text):
    with pytest.raises(ScraperError):
        scraper()._parse_browser_page(text, "engineer", 1)


@pytest.mark.parametrize("pages,maximum,pattern", [
    ([page_html()], 1, "max_pages"),
    ([page_html(), page_html(2, job_id=1)], 2, "repeated"),
    ([page_html(pages=1, total=10)], 2, "advertised"),
    ([page_html(), page_html(2, total=3)], 2, "changed"),
])
def test_incomplete_catalogue_fails_and_closes_browser(monkeypatch, pages, maximum, pattern):
    browser = install_browser(monkeypatch, pages)
    with pytest.raises(ScraperError, match=pattern):
        scraper(max_pages=maximum).fetch()
    assert browser.closed


@pytest.mark.parametrize("status", [403, 429, 500])
def test_blocked_browser_fails_and_closes(monkeypatch, status):
    browser = install_browser(monkeypatch, [page_html()], status)
    with pytest.raises(ScraperError, match=str(status)):
        scraper().fetch()
    assert browser.closed


def test_absent_dependency_is_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    with pytest.raises(ScraperError, match="cloakbrowser"):
        scraper().fetch()


def test_pipeline_preserves_previous_on_failures():
    from scripts.run_pipeline import CONFIGS
    assert all(CONFIGS["wellfound"][key] for key in (
        "fail_closed_on_any_error", "fail_closed_on_not_found", "fail_closed_on_empty",
    ))
