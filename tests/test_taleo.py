"""Tests for the Oracle Taleo Business Edition (TBE) scraper."""

from __future__ import annotations

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, TaleoScraper


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.taleo as tl
    monkeypatch.setattr(tl, "MAX_RETRIES", 1)
    monkeypatch.setattr(tl, "RETRY_BASE_DELAY", 0.0)


# Per-job JSON-LD detail enrichment fires after the listing parse.
# Tests that don't care about it leave those calls unmocked.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


URL = "https://phe.tbe.taleo.net/phe01/ats/careers/v2/searchResults?org=ACME&cws=41"


def _job_link(rid: str, title: str, base: str = "https://phe.tbe.taleo.net/phe01/ats/careers/v2") -> str:
    return (
        f'<h4 class="oracletaleocwsv2-head-title">'
        f'<a href="{base}/viewRequisition?org=ACME&cws=41&rid={rid}" '
        f'class="viewJobLink">{title}</a></h4>'
    )


def _page(links: list[str]) -> str:
    return f"<html><body><div class='oracletaleocwsv2-search-results'>{''.join(links)}</div></body></html>"


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_taleo() -> None:
    assert ScraperRegistry.get(ATSType.TALEO) is TaleoScraper


# --- URL validation ---------------------------------------------------------


def test_bare_slug_raises() -> None:
    """Taleo TBE shards (``ph{c}``) and instance numbers vary per tenant —
    we can't infer them from a bare slug, so the user must provide the full
    URL."""
    with pytest.raises(ScraperError, match="full URL"):
        TaleoScraper("acme").fetch()


def test_non_taleo_url_raises() -> None:
    with pytest.raises(ScraperError, match=r"tbe\.taleo\.net"):
        TaleoScraper("https://acme.example.com/careers").fetch()


# --- Happy path -------------------------------------------------------------


def test_parses_basic_listing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        text=_page([
            _job_link("100", "Senior Engineer"),
            _job_link("101", "Designer"),
        ]),
    )
    jobs = TaleoScraper(URL).fetch()
    assert len(jobs) == 2
    assert jobs[0].ats_id == "100"
    assert jobs[0].title == "Senior Engineer"
    assert jobs[0].company == "ACME"
    assert jobs[0].ats_type is ATSType.TALEO
    assert "rid=100" in str(jobs[0].url)


def test_returns_empty_when_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_page([]))
    assert TaleoScraper(URL).fetch() == []


def test_dedupes_by_rid(httpx_mock) -> None:
    """Each job typically has both a title link AND a 'View' button anchor —
    both have the ``viewJobLink`` class with the same ``rid``. Dedup."""
    httpx_mock.add_response(url=URL, text=_page([
        _job_link("100", "Senior Engineer"),
        _job_link("100", "View"),
    ]))
    jobs = TaleoScraper(URL).fetch()
    assert len(jobs) == 1
    # The first match wins (the title link, not the "View" button).
    assert jobs[0].title == "Senior Engineer"


def test_company_extracted_from_org_param(httpx_mock) -> None:
    url = "https://phh.tbe.taleo.net/phh04/ats/careers/v2/searchResults?org=PCG&cws=47"
    httpx_mock.add_response(url=url, text=_page([_job_link("1", "Job")]))
    jobs = TaleoScraper(url).fetch()
    assert jobs[0].company == "PCG"


def test_decodes_html_entities_in_url_and_title(httpx_mock) -> None:
    """Taleo amps + accented chars in titles must round-trip cleanly."""
    page = _page([
        '<h4 class="oracletaleocwsv2-head-title">'
        '<a href="https://phe.tbe.taleo.net/phe01/ats/careers/v2/viewRequisition?org=ACME&amp;cws=41&amp;rid=42" '
        'class="viewJobLink">R&amp;D Engineer</a></h4>'
    ])
    httpx_mock.add_response(url=URL, text=page)
    jobs = TaleoScraper(URL).fetch()
    assert jobs[0].title == "R&D Engineer"
    assert "&amp;" not in str(jobs[0].url)


def test_skips_anchor_with_empty_title(httpx_mock) -> None:
    page = _page([
        '<h4><a href="https://phe.tbe.taleo.net/phe01/ats/careers/v2/viewRequisition?org=ACME&cws=41&rid=99" '
        'class="viewJobLink">   </a></h4>',
        _job_link("100", "Real Job"),
    ])
    httpx_mock.add_response(url=URL, text=page)
    jobs = TaleoScraper(URL).fetch()
    assert [j.ats_id for j in jobs] == ["100"]


# --- Error handling ---------------------------------------------------------


def test_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        TaleoScraper(URL).fetch()


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.taleo as tl
    monkeypatch.setattr(tl, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, text=_page([_job_link("1", "X")]))
    jobs = TaleoScraper(URL).fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.taleo as tl
    monkeypatch.setattr(tl, "MAX_RETRIES", 2)
    httpx_mock.add_response(url=URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        TaleoScraper(URL).fetch()
