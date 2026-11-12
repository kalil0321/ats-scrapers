"""Tests for the Radancy TalentBrew career-site scraper."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.scrapers.talentbrew import (
    PAGE_SIZE,
    RESULT_CAP,
    TalentBrewScraper,
    _identifier,
    _job_posting,
    _normalize_host,
    _posting_location,
)

HOST = "jobs.example.com"
SEARCH = re.compile(r"^https://jobs\.example\.com/search-jobs/results\?")
DETAIL = re.compile(r"^https://jobs\.example\.com/job/")


def _row(job_id: str, title: str = "Security Officer", location: str = "Archbald, PA") -> str:
    return (
        f"<li><button data-job-id=\"{job_id}\" data-org-id=\"22950\">Save</button>"
        f"<a href=\"/job/archbald/security-officer/22950/{job_id}\" data-job-id=\"{job_id}\">"
        f"<h2>{title}</h2><span class=\"job-location\">{location}</span></a></li>"
    )


def _fragment(rows: list[str], total: int, pages: int = 1) -> dict:
    return {
        "filters": "",
        "hasJobs": bool(rows),
        "results": (
            f'<section id="search-results" data-total-results="{total}" '
            f'data-total-pages="{pages}"><ul>{"".join(rows)}</ul></section>'
        ),
    }


def _detail(**overrides) -> str:
    posting = {
        "@context": "http://schema.org",
        "@type": "JobPosting",
        "title": "Security Officer",
        "description": "&lt;p&gt;Protect the site.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Patrol&lt;/li&gt;&lt;li&gt;Report&lt;/li&gt;&lt;/ul&gt;",
        "datePosted": "2026-11-12",
        "employmentType": "Full Time",
        "identifier": {"@type": "PropertyValue", "name": "Job ID", "value": "2026-1747835"},
        "hiringOrganization": {"@type": "Organization", "name": "Example Security"},
        "jobLocation": [
            {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Archbald",
                    "addressRegion": "Pennsylvania",
                    "addressCountry": "US",
                },
            },
        ],
    }
    posting.update(overrides)
    return (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps({"@type": "WebSite"})
        + "</script><script type=\"application/ld+json\">"
        + json.dumps(posting)
        + "</script></head><body></body></html>"
    )


def test_is_registered() -> None:
    assert ScraperRegistry.get(ATSType.TALENTBREW) is TalentBrewScraper


def test_listing_only(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([_row("103")], 1))

    jobs = TalentBrewScraper(HOST, company_name="Example", include_descriptions=False).fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert str(job.url) == "https://jobs.example.com/job/archbald/security-officer/22950/103"
    assert job.ats_type is ATSType.TALENTBREW
    assert job.ats_id == "jobs.example.com:103"
    assert job.title == "Security Officer"
    assert job.company == "Example"
    assert job.location == "Archbald, PA"
    assert job.description is None
    request = httpx_mock.get_requests()[0]
    query = parse_qs(urlparse(str(request.url)).query, keep_blank_values=True)
    assert query["CurrentPage"] == ["1"]
    assert query["RecordsPerPage"] == [str(PAGE_SIZE)]
    assert query["SearchResultsModuleName"] == ["Search Results"]


def test_details_fill_the_posting(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([_row("103")], 1))
    httpx_mock.add_response(url=DETAIL, text=_detail())

    job = TalentBrewScraper(HOST).fetch()[0]

    assert job.company == "Example Security"
    assert job.description == "Protect the site.\n\n- Patrol\n- Report"
    assert job.posted_at == datetime(2026, 11, 12, tzinfo=UTC)
    assert job.location == "Archbald, Pennsylvania, US"
    assert job.country_iso == "US"
    assert job.commitment == "Full Time"
    assert job.employment_type == "FULL_TIME"
    assert job.requisition_id == "2026-1747835"


def test_seed_name_beats_the_page(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([_row("103")], 1))
    httpx_mock.add_response(url=DETAIL, text=_detail())

    job = TalentBrewScraper(HOST, company_name="Example").fetch()[0]

    assert job.company == "Example"


def test_unreadable_detail_keeps_the_listing_row(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([_row("103"), _row("104")], 2))
    httpx_mock.add_response(url=re.compile(r".*/22950/103$"), text="<html>no data</html>")
    httpx_mock.add_response(url=re.compile(r".*/22950/104$"), status_code=404)

    jobs = TalentBrewScraper(HOST).fetch()

    assert sorted(job.ats_id for job in jobs) == ["jobs.example.com:103", "jobs.example.com:104"]
    assert all(job.description is None for job in jobs)


def test_get_description_fetches_one_page(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([_row("103")], 1))
    scraper = TalentBrewScraper(HOST, include_descriptions=False)
    job = scraper.fetch()[0]
    httpx_mock.add_response(url=DETAIL, text=_detail())

    assert scraper.get_description(job) == "Protect the site.\n\n- Patrol\n- Report"
    assert job.description is None


def test_walks_every_page(httpx_mock) -> None:
    total = PAGE_SIZE + 3

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["CurrentPage"][0])
        first = (page - 1) * PAGE_SIZE
        rows = [_row(str(1000 + i)) for i in range(first, min(first + PAGE_SIZE, total))]
        return httpx.Response(200, json=_fragment(rows, total, pages=2))

    httpx_mock.add_callback(reply, url=SEARCH, is_reusable=True)

    jobs = TalentBrewScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == total
    assert len(httpx_mock.get_requests()) == 2


def test_stops_at_the_result_cap(httpx_mock) -> None:
    """Above 10,000 postings the search returns nothing; the walk ends there."""
    total = RESULT_CAP + 857
    last = RESULT_CAP // PAGE_SIZE

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["CurrentPage"][0])
        first = (page - 1) * PAGE_SIZE
        rows = [_row(str(1000 + i)) for i in range(first, first + PAGE_SIZE)]
        return httpx.Response(200, json=_fragment(rows, total, pages=last + 2))

    httpx_mock.add_callback(reply, url=SEARCH, is_reusable=True)

    jobs = TalentBrewScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == RESULT_CAP
    assert len(httpx_mock.get_requests()) == last


def test_site_with_its_own_page_size_is_walked_to_the_end(httpx_mock) -> None:
    """A site that ignores RecordsPerPage reports more, smaller pages."""
    total, size = 45, 15

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["CurrentPage"][0])
        rows = [_row(str(1000 + i)) for i in range((page - 1) * size, min(page * size, total))]
        return httpx.Response(200, json=_fragment(rows, total, pages=3))

    httpx_mock.add_callback(reply, url=SEARCH, is_reusable=True)

    jobs = TalentBrewScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == total
    assert len(httpx_mock.get_requests()) == 3


def test_empty_page_inside_the_walk_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([_row("103")], 3, pages=3))
    httpx_mock.add_response(url=SEARCH, json=_fragment([], 3, pages=3))

    with pytest.raises(ScraperError, match="empty page 2 of 3"):
        TalentBrewScraper(HOST, include_descriptions=False).fetch()


def test_locale_prefixed_job_links_are_read(httpx_mock) -> None:
    row = '<li><a href="/en/job/norfolk/engineer/4832/100" data-job-id="100"><h2>Engineer</h2></a></li>'
    httpx_mock.add_response(url=SEARCH, json=_fragment([row], 1))

    job = TalentBrewScraper(HOST, include_descriptions=False).fetch()[0]

    assert str(job.url) == "https://jobs.example.com/en/job/norfolk/engineer/4832/100"


def test_empty_site_returns_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([], 0, pages=0))

    assert TalentBrewScraper(HOST).fetch() == []


def test_fails_closed_on_a_short_walk(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH, json=_fragment([_row("103")], 40))

    with pytest.raises(ScraperError, match="returned 1 of 40"):
        TalentBrewScraper(HOST, include_descriptions=False).fetch()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"results": None},
        {"results": "<ul></ul>"},
        {"results": '<section data-total-results="many"></section>'},
        {"results": '<section data-total-results="-1"></section>'},
    ],
)
def test_rejects_unexpected_payloads(httpx_mock, payload) -> None:
    httpx_mock.add_response(url=SEARCH, json=payload)

    with pytest.raises(ScraperError):
        TalentBrewScraper(HOST).fetch()


def test_rows_with_mismatched_ids_or_foreign_links_are_ignored(httpx_mock) -> None:
    rows = [
        _row("103"),
        '<li><a href="/job/a/b/22950/999" data-job-id="104"><h2>Wrong id</h2></a></li>',
        '<li><a href="https://evil.example/job/a/b/1/105" data-job-id="105"><h2>Foreign</h2></a></li>',
        '<li><a href="/job/a/b/22950/106" data-job-id="106"><h2> </h2></a></li>',
    ]
    httpx_mock.add_response(url=SEARCH, json=_fragment(rows, 1))

    jobs = TalentBrewScraper(HOST, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["jobs.example.com:103"]


def test_location_next_to_the_link_is_found(httpx_mock) -> None:
    row = (
        '<li><span class="sr-job-location">Toronto, Canada</span>'
        '<h3><a class="sr-job-item__link" href="/job/toronto/director/287/998" '
        'data-job-id="998">Director</a></h3></li>'
    )
    httpx_mock.add_response(url=SEARCH, json=_fragment([row], 1))

    job = TalentBrewScraper(HOST, include_descriptions=False).fetch()[0]

    assert job.title == "Director"
    assert job.location == "Toronto, Canada"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("jobs.example.com", "jobs.example.com"),
        ("https://Jobs.Example.com/search-jobs", "jobs.example.com"),
    ],
)
def test_normalize_host(value: str, expected: str) -> None:
    assert _normalize_host(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "localhost", "http://jobs.example.com", "https://u@jobs.example.com", "https://jobs.example.com:8443"],
)
def test_normalize_host_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_host(value)


def test_job_posting_skips_broken_and_unrelated_blocks() -> None:
    page = (
        '<script type="application/ld+json">{broken</script>'
        '<script type="application/ld+json">[{"@type": "BreadcrumbList"}]</script>'
        '<script type="application/ld+json">{"@type": "JobPosting", "title": "X"}</script>'
    )

    assert _job_posting(page) == {"@type": "JobPosting", "title": "X"}
    assert _job_posting("<html></html>") is None


def test_posting_location_handles_several_places() -> None:
    posting = {
        "jobLocation": [
            {"address": {"addressLocality": "Toronto", "addressRegion": "Ontario", "addressCountry": "CA"}},
            {"address": {"addressLocality": "Buffalo", "addressRegion": "New York", "addressCountry": {"name": "US"}}},
            {"address": "unstructured"},
        ],
    }

    assert _posting_location(posting) == ("Toronto, Ontario, CA | Buffalo, New York, US", None)
    assert _posting_location({"jobLocation": {"address": {"addressCountry": "de"}}}) == ("de", "DE")
    assert _posting_location({}) == (None, None)


def test_job_posting_reads_type_lists_and_graphs() -> None:
    typed = '<script type="application/ld+json">{"@type": ["Thing", "JobPosting"], "title": "A"}</script>'
    graph = (
        '<script type="application/ld+json">'
        '{"@context": "https://schema.org", "@graph": [{"@type": "WebSite"}, {"@type": "JobPosting", "title": "B"}]}'
        "</script>"
    )

    assert _job_posting(typed) == {"@type": ["Thing", "JobPosting"], "title": "A"}
    assert _job_posting(graph) == {"@type": "JobPosting", "title": "B"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"@type": "PropertyValue", "value": " R-12 "}, "R-12"),
        ({"value": 4711}, "4711"),
        ("REQ 9", "REQ 9"),
        (4711, "4711"),
        ({"name": "Job ID"}, None),
        (True, None),
        (None, None),
    ],
)
def test_identifier_accepts_text_numbers_and_property_values(value, expected) -> None:
    assert _identifier(value) == expected


def test_rows_without_a_title_do_not_count_as_postings(httpx_mock) -> None:
    untitled = [_row(str(i), title="") for i in range(200, 240)]
    httpx_mock.add_response(url=SEARCH, json=_fragment(untitled, 40))

    with pytest.raises(ScraperError, match="returned 0 of 40"):
        TalentBrewScraper(HOST, include_descriptions=False).fetch()


@pytest.mark.parametrize(("returned", "raises"), [(5, True), (6, False)])
def test_coverage_floor_rounds_up(httpx_mock, returned: int, raises: bool) -> None:
    # 60% of 9 announced postings is 5.4, so 6 are needed.
    rows = [_row(str(300 + i)) for i in range(returned)]
    httpx_mock.add_response(url=SEARCH, json=_fragment(rows, 9))
    scraper = TalentBrewScraper(HOST, include_descriptions=False)

    if raises:
        with pytest.raises(ScraperError, match="returned 5 of 9"):
            scraper.fetch()
    else:
        assert len(scraper.fetch()) == 6


def test_capped_site_logs_the_announced_total(httpx_mock, caplog) -> None:
    pages = RESULT_CAP // PAGE_SIZE
    for page in range(1, pages + 1):
        rows = [_row(str(page * 1_000 + i)) for i in range(PAGE_SIZE)]
        httpx_mock.add_response(url=SEARCH, json=_fragment(rows, 25_000, pages + 30))

    with caplog.at_level("WARNING"):
        jobs = TalentBrewScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == RESULT_CAP
    assert "announces 25000 postings" in caplog.text
