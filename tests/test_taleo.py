"""Tests for the Oracle Taleo Business Edition (TBE) scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, TaleoScraper

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py — the shared fetch layer replaced per-scraper retry constants.


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


def test_5xx_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, text=_page([_job_link("1", "X")]))
    jobs = TaleoScraper(URL).fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch
    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 2)
    httpx_mock.add_response(url=URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        TaleoScraper(URL).fetch()


# --- Paging -----------------------------------------------------------------
#
# The listing shows ten postings at a time. While more remain, the page
# carries the link its infinite scroll follows; the last page has none.


def _next_link(row: int, *, amp: str = "&") -> str:
    return (
        f'<a href="/phe01/ats/careers/v2/searchResults?next{amp}rowFrom={row}'
        f'{amp}act=null{amp}sortColumn=null{amp}sortOrder=null'
        f'{amp}currentTime=1794514824131">Next</a>'
    )


def _listing_requests(httpx_mock) -> list[str]:
    return [
        str(request.url)
        for request in httpx_mock.get_requests()
        if "searchResults" in str(request.url)
    ]


def test_follows_the_next_link_to_the_last_page(httpx_mock) -> None:
    first = [_job_link(str(rid), f"Job {rid}") for rid in range(100, 110)]
    second = [_job_link(str(rid), f"Job {rid}") for rid in range(110, 120)]
    third = [_job_link(str(rid), f"Job {rid}") for rid in range(120, 123)]
    httpx_mock.add_response(url=URL, text=_page([*first, _next_link(10)]))
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10", text=_page([*second, _next_link(20)])
    )
    httpx_mock.add_response(url=f"{URL}&rowFrom=20", text=_page(third))

    jobs = TaleoScraper(URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == [str(rid) for rid in range(100, 123)]
    assert _listing_requests(httpx_mock) == [
        URL,
        f"{URL}&rowFrom=10",
        f"{URL}&rowFrom=20",
    ]


def test_single_page_listing_makes_one_request(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_page([_job_link("1", "Only job")]))

    jobs = TaleoScraper(URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1"]
    assert _listing_requests(httpx_mock) == [URL]


def test_offset_is_read_from_the_link_not_assumed(httpx_mock) -> None:
    """A tenant showing a different page size still pages correctly."""
    httpx_mock.add_response(
        url=URL,
        text=_page([_job_link("1", "A"), _job_link("2", "B"), _next_link(25)]),
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=25", text=_page([_job_link("3", "C")])
    )

    jobs = TaleoScraper(URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3"]


def test_next_link_with_encoded_ampersands(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        text=_page([_job_link("1", "A"), _next_link(10, amp="&amp;")]),
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10", text=_page([_job_link("2", "B")])
    )

    jobs = TaleoScraper(URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]


def test_posting_repeated_on_the_next_page_is_kept_once(httpx_mock) -> None:
    """A posting can slide onto the next page while the walk is running."""
    httpx_mock.add_response(
        url=URL,
        text=_page([_job_link("1", "A"), _job_link("2", "B"), _next_link(10)]),
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10",
        text=_page([_job_link("2", "B"), _job_link("3", "C")]),
    )

    jobs = TaleoScraper(URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3"]


def test_last_page_of_known_postings_ends_the_walk(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        text=_page([_job_link("1", "A"), _job_link("2", "B"), _next_link(10)]),
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10", text=_page([_job_link("2", "B")])
    )

    jobs = TaleoScraper(URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]


def test_page_that_repeats_postings_and_announces_more_raises(httpx_mock) -> None:
    page = _page([_job_link("1", "A"), _job_link("2", "B"), _next_link(10)])
    httpx_mock.add_response(url=URL, text=page)
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10",
        text=_page([_job_link("1", "A"), _job_link("2", "B"), _next_link(20)]),
    )

    with pytest.raises(ScraperError, match="paging stalled at row 10"):
        TaleoScraper(URL, include_descriptions=False).fetch()


def test_next_link_that_does_not_move_forward_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL, text=_page([_job_link("1", "A"), _next_link(10)])
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10",
        text=_page([_job_link("2", "B"), _next_link(10)]),
    )

    with pytest.raises(ScraperError, match="paging stalled at row 10"):
        TaleoScraper(URL, include_descriptions=False).fetch()


def test_announced_page_without_postings_raises(httpx_mock) -> None:
    """Taleo serves its error page with a 200: an announced page that
    lists nothing must not pass for the end of the list."""
    httpx_mock.add_response(
        url=URL, text=_page([_job_link("1", "A"), _next_link(10)])
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10",
        text="<html><body>The page you requested was not found.</body></html>",
    )

    with pytest.raises(ScraperError, match="returned no postings at row 10"):
        TaleoScraper(URL, include_descriptions=False).fetch()


def test_page_guard_raises_instead_of_returning_a_partial_list(
    monkeypatch, httpx_mock
) -> None:
    from ats_scrapers.scrapers import taleo

    monkeypatch.setattr(taleo, "MAX_PAGES", 2)
    httpx_mock.add_response(
        url=URL, text=_page([_job_link("1", "A"), _next_link(10)])
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10",
        text=_page([_job_link("2", "B"), _next_link(20)]),
    )

    with pytest.raises(ScraperError, match="page guard 2"):
        TaleoScraper(URL, include_descriptions=False).fetch()


def test_failed_page_fails_the_whole_walk(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch

    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 2)
    httpx_mock.add_response(
        url=URL, text=_page([_job_link("1", "A"), _next_link(10)])
    )
    httpx_mock.add_response(
        url=f"{URL}&rowFrom=10", status_code=502, is_reusable=True
    )

    with pytest.raises(ScraperError, match="502"):
        TaleoScraper(URL, include_descriptions=False).fetch()


def test_page_url_for_a_listing_without_a_query() -> None:
    from ats_scrapers.scrapers.taleo import _page_url

    base = "https://phe.tbe.taleo.net/phe01/ats/careers/v2/searchResults"
    assert _page_url(base, 0) == base
    assert _page_url(base, 10) == f"{base}?rowFrom=10"
    assert _page_url(URL, 30) == f"{URL}&rowFrom=30"
