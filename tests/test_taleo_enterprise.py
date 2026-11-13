"""Tests for the Oracle Taleo Enterprise Edition career-section scraper."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import quote

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, TaleoEnterpriseScraper
from ats_scrapers.scrapers import taleo_enterprise as module
from ats_scrapers.scrapers.taleo_enterprise import (
    _description,
    _parse_date,
    _parse_slug,
    _places,
    _search_payload,
)

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py.

SLUG = "acme.taleo.net/ex"
SECTION = "https://acme.taleo.net/careersection/ex"
SEARCH_PAGE = f"{SECTION}/jobsearch.ftl?lang=en"
API = "https://acme.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal=101430233"


def _search_page(portal: str = "101430233") -> str:
    return (
        "<html><head><title>Job Search</title></head><body><script>"
        f"var api = '/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}';"
        "</script></body></html>"
    )


def _row(
    contest: str,
    title: str,
    *,
    places: list[str] | None = None,
    date: str = "Nov 12, 2026",
    job_id: str | None = None,
) -> dict[str, object]:
    return {
        "jobId": job_id or f"9{contest}",
        "contestNo": contest,
        "column": [title, json.dumps(places or ["US-TX-Houston"]), date],
        "linkedColumn": 0,
        "locationsColumns": [1],
    }


def _payload(rows: list[dict[str, object]], total: int, page: int = 1) -> dict[str, object]:
    return {
        "requisitionList": rows,
        "pagingData": {"currentPageNo": page, "pageSize": 25, "totalCount": total},
        "careerSectionUnAvailable": False,
    }


def _detail_page(*values: str) -> str:
    fields = ["1", "Submission for the position", *values, "Apply"]
    encoded = "!|!".join(quote(field, safe="") for field in fields)
    return (
        "<html><head><title>Acme - Careers</title></head><body>"
        f'<input type="hidden" name="initialHistory" id="initialHistory" value="{encoded}" />'
        "</body></html>"
    )


def _posts(httpx_mock) -> list[dict[str, object]]:
    return [
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if request.method == "POST"
    ]


# --- Registry and slug ------------------------------------------------------


def test_registry_resolves_taleo_enterprise() -> None:
    assert ScraperRegistry.get(ATSType.TALEO_ENTERPRISE) is TaleoEnterpriseScraper


@pytest.mark.parametrize(
    "slug",
    [
        "acme.taleo.net/ex",
        "ACME.taleo.net/ex/",
        "https://acme.taleo.net/careersection/ex/jobsearch.ftl?lang=en",
        "https://acme.taleo.net/careersection/ex",
        "https://acme.taleo.net/careersection/ex/jobdetail.ftl?job=1",
    ],
)
def test_slug_forms(slug: str) -> None:
    assert _parse_slug(slug) == ("acme.taleo.net", "ex")


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "acme",
        "acme.taleo.net",
        "acme.taleo.net/",
        "acme.tbe.taleo.net/ex",  # Business Edition is a different product
        "acme.example.com/ex",
        "evil.com/x?y=.taleo.net/ex",
        "acme.taleo.net/ex/../admin",
        "acme.taleo.net/rest",
        "http://acme.taleo.net/careersection/ex/jobsearch.ftl",
        "https://user:pw@acme.taleo.net/careersection/ex/jobsearch.ftl",
        "https://acme.taleo.net:8443/careersection/ex/jobsearch.ftl",
        "https://acme.taleo.net/other/ex/jobsearch.ftl",
        "https://acme.taleo.net.evil.com/careersection/ex/jobsearch.ftl",
    ],
)
def test_invalid_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(ScraperError):
        TaleoEnterpriseScraper(slug)


# --- Listing ----------------------------------------------------------------


def test_parses_a_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(
        url=API,
        method="POST",
        json=_payload(
            [
                _row("26001GK", "Plant Manager", places=["US-SD-Aurora"]),
                _row("26002", "Welder &amp; Fitter", places=["US-TX-Houston", "US-LA-Norco"]),
            ],
            total=2,
        ),
    )

    jobs = TaleoEnterpriseScraper(
        SLUG, company_name="Acme Corp", include_descriptions=False
    ).fetch()

    assert [job.title for job in jobs] == ["Plant Manager", "Welder & Fitter"]
    first = jobs[0]
    assert first.company == "Acme Corp"
    assert first.ats_type is ATSType.TALEO_ENTERPRISE
    assert first.ats_id == "acme:26001GK"
    assert first.requisition_id == "26001GK"
    assert str(first.url) == f"{SECTION}/jobdetail.ftl?job=26001GK&lang=en"
    assert first.location == "US-SD-Aurora"
    assert first.posted_at == datetime(2026, 11, 12, tzinfo=UTC)
    assert first.description is None
    assert jobs[1].location == "US-TX-Houston; US-LA-Norco"


def test_search_request_matches_the_form(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([_row("1", "A")], total=1))

    TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    request = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(request.content)
    assert body == _search_payload(1)
    assert body["pageNo"] == 1
    assert body["fieldData"]["fields"] == {"KEYWORD": "", "LOCATION": ""}
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert request.headers["tz"] == "GMT+00:00"
    assert request.headers["Referer"] == SEARCH_PAGE
    assert request.headers["Accept-Encoding"] == "identity"


def test_company_falls_back_to_the_zone(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([_row("1", "A")], total=1))

    jobs = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    assert jobs[0].company == "acme"


def test_empty_section_returns_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([], total=0))

    assert TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch() == []


def test_title_and_location_positions_come_from_the_payload(httpx_mock) -> None:
    """Sections lay their columns out differently."""
    row = {
        "jobId": "77",
        "contestNo": "NEW015884",
        "column": [
            "Hotel Central Park",
            "Pot Washer",
            "Stewarding",
            '["US-NY-New York City"]',
            "Nov 12, 2026",
        ],
        "linkedColumn": 1,
        "locationsColumns": [3],
    }
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([row], total=1))

    job = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()[0]

    assert job.title == "Pot Washer"
    assert job.location == "US-NY-New York City"
    assert job.posted_at == datetime(2026, 11, 12, tzinfo=UTC)
    assert job.raw is not None
    assert job.raw["contestNo"] == "NEW015884"


def test_row_without_location_or_date_columns(httpx_mock) -> None:
    row = {"jobId": "5", "contestNo": "R5", "column": ["Analyst"], "linkedColumn": 0}
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([row], total=1))

    job = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()[0]

    assert job.title == "Analyst"
    assert job.location is None
    assert job.posted_at is None


def test_job_id_is_used_when_there_is_no_requisition_number(httpx_mock) -> None:
    row = {"jobId": 4711, "column": ["Analyst"], "linkedColumn": 0}
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([row], total=1))

    job = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()[0]

    assert job.ats_id == "acme:4711"
    assert job.requisition_id is None
    assert str(job.url).endswith("jobdetail.ftl?job=4711&lang=en")


def test_requisition_number_is_escaped_in_the_url(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(
        url=API, method="POST", json=_payload([_row("A/B 1&x", "Analyst")], total=1)
    )

    job = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()[0]

    assert str(job.url) == f"{SECTION}/jobdetail.ftl?job=A%2FB%201%26x&lang=en"
    assert job.ats_id == "acme:A/B 1&x"


# --- Paging -----------------------------------------------------------------


def test_reads_every_page(httpx_mock) -> None:
    first = [_row(str(n), f"Job {n}") for n in range(25)]
    second = [_row(str(n), f"Job {n}") for n in range(25, 50)]
    third = [_row(str(n), f"Job {n}") for n in range(50, 53)]
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload(first, total=53))
    httpx_mock.add_response(url=API, method="POST", json=_payload(second, total=53, page=2))
    httpx_mock.add_response(url=API, method="POST", json=_payload(third, total=53, page=3))

    jobs = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    assert [job.requisition_id for job in jobs] == [str(n) for n in range(53)]
    assert [body["pageNo"] for body in _posts(httpx_mock)] == [1, 2, 3]


def test_single_page_makes_one_search_request(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(
        url=API, method="POST", json=_payload([_row("1", "A"), _row("2", "B")], total=2)
    )

    TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    assert len(_posts(httpx_mock)) == 1


def test_posting_repeated_across_pages_is_kept_once(httpx_mock) -> None:
    first = [_row(str(n), f"Job {n}") for n in range(25)]
    second = [_row("24", "Job 24"), _row("25", "Job 25")]
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload(first, total=27))
    httpx_mock.add_response(url=API, method="POST", json=_payload(second, total=27, page=2))

    jobs = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    assert len(jobs) == 26
    assert len({job.ats_id for job in jobs}) == 26


def test_pages_that_mostly_repeat_earlier_postings_are_not_a_failure(httpx_mock) -> None:
    """On some sections most rows of the later pages are requisitions
    already listed: 200 rows for 125 postings on one of them."""
    first = [_row(str(n), f"Job {n}") for n in range(25)]
    repeat = [_row(str(n), f"Job {n}") for n in range(5, 25)]
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload(first, total=75))
    httpx_mock.add_response(url=API, method="POST", json=_payload(repeat, total=75, page=2))
    httpx_mock.add_response(url=API, method="POST", json=_payload(repeat, total=75, page=3))

    jobs = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    assert [job.requisition_id for job in jobs] == [str(n) for n in range(25)]


def test_empty_page_in_the_middle_does_not_end_the_walk(httpx_mock) -> None:
    """The announced total can exceed the postings the pages hold, and a
    page can come back empty with more postings after it."""
    first = [_row(str(n), f"Job {n}") for n in range(25)]
    fourth = [_row(str(n), f"Job {n}") for n in range(25, 33)]
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload(first, total=100))
    httpx_mock.add_response(url=API, method="POST", json=_payload([], total=100, page=2))
    httpx_mock.add_response(
        url=API, method="POST", json=_payload([_row("24", "Job 24")], total=100, page=3)
    )
    httpx_mock.add_response(url=API, method="POST", json=_payload(fourth, total=100, page=4))

    jobs = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    assert [job.requisition_id for job in jobs] == [str(n) for n in range(33)]
    assert [body["pageNo"] for body in _posts(httpx_mock)] == [1, 2, 3, 4]


def test_page_count_uses_the_announced_page_size(httpx_mock) -> None:
    payload = _payload([_row(str(n), f"Job {n}") for n in range(10)], total=20)
    payload["pagingData"]["pageSize"] = 10
    second = _payload([_row(str(n), f"Job {n}") for n in range(10, 20)], total=20, page=2)
    second["pagingData"]["pageSize"] = 10
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=payload)
    httpx_mock.add_response(url=API, method="POST", json=second)

    jobs = TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()

    assert len(jobs) == 20
    assert [body["pageNo"] for body in _posts(httpx_mock)] == [1, 2]


def test_announced_postings_but_none_listed_is_an_empty_section(httpx_mock) -> None:
    """Some sections announce a few postings and list none, on the site
    as well: that is an empty section, not a failed walk."""
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([], total=30))
    httpx_mock.add_response(url=API, method="POST", json=_payload([], total=30, page=2))

    assert TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch() == []
    assert [body["pageNo"] for body in _posts(httpx_mock)] == [1, 2]


def test_rows_that_do_not_parse_count_against_coverage(httpx_mock) -> None:
    rows = [{"jobId": str(n), "contestNo": str(n), "column": [""]} for n in range(10)]
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload(rows, total=10))

    with pytest.raises(ScraperError, match="could read only 0 of the 10 postings listed"):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()


def test_coverage_floor_is_rounded_up(httpx_mock) -> None:
    """Nine of eleven is below 90%."""
    rows = [_row(str(n), f"Job {n}") for n in range(9)]
    rows += [{"jobId": "x", "contestNo": "x", "column": [""]}] * 2
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload(rows, total=11))

    with pytest.raises(ScraperError, match="could read only 9 of the 11 postings listed"):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()


def test_page_guard_raises_instead_of_returning_a_partial_list(
    monkeypatch, httpx_mock
) -> None:
    monkeypatch.setattr(module, "MAX_PAGES", 2)
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(
        url=API,
        method="POST",
        json=_payload([_row(str(n), f"Job {n}") for n in range(25)], total=75),
    )

    with pytest.raises(ScraperError, match="page guard 2"):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()
    # The guard trips on the first response, before walking further.
    assert len(_posts(httpx_mock)) == 1


def test_failed_page_fails_the_whole_walk(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch

    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 2)
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(
        url=API,
        method="POST",
        json=_payload([_row(str(n), f"Job {n}") for n in range(25)], total=40),
    )
    httpx_mock.add_response(url=API, method="POST", status_code=502, is_reusable=True)

    with pytest.raises(ScraperError, match="502"):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()


# --- Unexpected responses ---------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"pagingData": {"totalCount": 1}},
        {"requisitionList": [], "pagingData": {}},
        {"requisitionList": [], "pagingData": {"totalCount": "12"}},
        {"requisitionList": [], "pagingData": {"totalCount": True}},
        {"requisitionList": [], "pagingData": {"totalCount": -1}},
        {"requisitionList": {"a": 1}, "pagingData": {"totalCount": 1}},
        {"requisitionList": ["row"], "pagingData": {"totalCount": 1}},
    ],
)
def test_unexpected_search_response_raises(httpx_mock, payload) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=payload)

    with pytest.raises(ScraperError):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()


def test_unavailable_flag_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(
        url=API,
        method="POST",
        json={"requisitionList": [], "pagingData": None, "careerSectionUnAvailable": True},
    )

    with pytest.raises(CompanyNotFoundError):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()


def test_unavailable_section_page_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SEARCH_PAGE,
        text="<html><head><title>Career Section Unavailable</title></head></html>",
    )

    with pytest.raises(CompanyNotFoundError, match="not available"):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()


def test_search_page_without_portal_number_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SEARCH_PAGE,
        text="<html><head><title>Privacy Agreement</title></head></html>",
    )

    with pytest.raises(ScraperError, match="no portal number") as excinfo:
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        TaleoEnterpriseScraper(SLUG, include_descriptions=False).fetch()


# --- Descriptions -----------------------------------------------------------


def test_description_is_read_from_the_detail_page(httpx_mock) -> None:
    detail = _detail_page(
        "!*!<p>We make <strong>anvils</strong>.</p><ul><li>Lift\\: 20 kg</li></ul>",
        "!*!<p>We make <strong>anvils</strong>.</p><ul><li>Lift\\: 20 kg</li></ul>",
        "!*!<p>Requirements &amp; skills</p>",
        "Tell us about a friend",
    )
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([_row("7", "Smith")], total=1))
    httpx_mock.add_response(url=f"{SECTION}/jobdetail.ftl?job=7&lang=en", text=detail)

    job = TaleoEnterpriseScraper(SLUG).fetch()[0]

    assert job.description == "We make anvils .\nLift: 20 kg\n\nRequirements & skills"


def test_get_description_fetches_the_detail_page(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([_row("7", "Smith")], total=1))
    httpx_mock.add_response(
        url=f"{SECTION}/jobdetail.ftl?job=7&lang=en",
        text=_detail_page("!*!<p>Forge things.</p>"),
    )
    scraper = TaleoEnterpriseScraper(SLUG, include_descriptions=False)
    job = scraper.fetch()[0]
    assert job.description is None

    assert scraper.get_description(job) == "Forge things."
    assert job.description == "Forge things."


def test_failed_detail_page_keeps_the_posting(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch

    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 1)
    httpx_mock.add_response(url=SEARCH_PAGE, text=_search_page())
    httpx_mock.add_response(url=API, method="POST", json=_payload([_row("7", "Smith")], total=1))
    httpx_mock.add_response(url=f"{SECTION}/jobdetail.ftl?job=7&lang=en", status_code=500)

    jobs = TaleoEnterpriseScraper(SLUG).fetch()

    assert [job.title for job in jobs] == ["Smith"]
    assert jobs[0].description is None


def test_description_helper_edge_cases() -> None:
    assert _description("<html></html>") is None
    assert _description('<input id="initialHistory" />') is None
    assert _description(_detail_page("plain", "values", "only")) is None
    # Attribute order does not matter.
    page = '<input value="%21%2A%21%3Cp%3EHi%3C%2Fp%3E" type="hidden" id="initialHistory">'
    assert _description(page) == "Hi"
    long_text = "!*!<p>" + "x" * 40_000 + "</p>"
    assert len(_description(_detail_page(long_text)) or "") == module.MAX_DESCRIPTION_CHARS


# --- Small helpers ----------------------------------------------------------


def test_places() -> None:
    assert _places('["US-SD-Aurora"]') == ["US-SD-Aurora"]
    assert _places('["A", "B", ""]') == ["A", "B"]
    assert _places("Multiple Locations") == ["Multiple Locations"]
    assert _places("") == []
    assert _places(None) == []
    assert _places("[1, null]") == ["1"]


def test_parse_date() -> None:
    assert _parse_date("Nov 12, 2026") == datetime(2026, 11, 12, tzinfo=UTC)
    assert _parse_date("November 3, 2026") == datetime(2026, 11, 3, tzinfo=UTC)
    assert _parse_date("3 Nov 2026") == datetime(2026, 11, 3, tzinfo=UTC)
    assert _parse_date("2026-11-03") == datetime(2026, 11, 3, tzinfo=UTC)
    assert _parse_date("Stewarding") is None
    assert _parse_date("") is None
    assert _parse_date(None) is None
