"""Tests for the Jibe (iCIMS career site) scraper."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.fetch import MalformedJSONError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.scrapers.jibe import (
    PAGE_SIZE,
    JibeScraper,
    _normalize_host,
    _parse_datetime,
    _states_from_locations,
)

HOST = "careers.example.com"
API = re.compile(r"^https://careers\.example\.com/api/jobs\?")


def _posting(slug: str, **overrides) -> dict:
    data = {
        "slug": slug,
        "req_id": slug,
        "title": f"Cashier {slug}",
        "description": "<p>Serve members.</p><ul><li>Scan items</li><li>Take payment</li></ul>",
        "responsibilities": "<p>Keep the lane tidy.</p>",
        "qualifications": "",
        "city": "Winter Park",
        "state": "Florida",
        "country": "United States",
        "country_code": "US",
        "full_location": "Winter Park, Florida",
        "location_name": "East Orlando",
        "postal_code": "32792",
        "posted_date": "2026-07-28T21:27:00+0000",
        "categories": [{"name": "Warehouses"}, {"name": "Front End"}],
        "employment_type": "PART_TIME",
        "language": "en-us",
        "hiring_organization": "Example Wholesale",
        "apply_url": f"https://careers-example.icims.com/jobs/{slug}/login",
        "ats_code": "icims",
    }
    data.update(overrides)
    return {"data": data}


def _payload(rows: list[dict], total: int, **extra) -> dict:
    return {"jobs": rows, "totalCount": total, **extra}


def test_is_registered() -> None:
    assert ScraperRegistry.get(ATSType.JIBE) is JibeScraper


def test_maps_a_posting(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_payload([_posting("59475")], 1))

    jobs = JibeScraper(HOST).fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert str(job.url) == "https://careers.example.com/jobs/59475?lang=en-us"
    assert job.ats_type is ATSType.JIBE
    assert job.ats_id == "careers.example.com:59475"
    assert job.requisition_id == "59475"
    assert job.title == "Cashier 59475"
    assert job.company == "Example Wholesale"
    assert job.location == "Winter Park, Florida, United States"
    assert job.country_iso == "US"
    assert job.department == "Warehouses"
    assert job.employment_type == "PART_TIME"
    assert job.commitment == "PART_TIME"
    assert job.language == "en"
    assert str(job.apply_url) == "https://careers-example.icims.com/jobs/59475/login"
    assert job.posted_at == datetime(2026, 7, 28, 21, 27, tzinfo=UTC)
    assert job.description == (
        "Serve members.\n\n- Scan items\n- Take payment\n\n"
        "Responsibilities\nKeep the lane tidy."
    )
    assert job.raw == {
        "location_name": "East Orlando",
        "postal_code": "32792",
        "ats_code": "icims",
        "categories": ["Warehouses", "Front End"],
    }


def test_company_name_from_the_seed_wins(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_payload([_posting("1")], 1))

    jobs = JibeScraper(f"https://{HOST}/", company_name="  Example  ").fetch()

    assert jobs[0].company == "Example"


def test_descriptions_can_be_skipped(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_payload([_posting("1")], 1))

    jobs = JibeScraper(HOST, include_descriptions=False).fetch()

    assert jobs[0].description is None


def test_empty_board_returns_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_payload([], 0))

    assert JibeScraper(HOST).fetch() == []


def test_walks_every_page(httpx_mock) -> None:
    total = PAGE_SIZE * 2 + 5

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        first = (page - 1) * PAGE_SIZE
        rows = [_posting(str(i)) for i in range(first, min(first + PAGE_SIZE, total))]
        return httpx.Response(200, json=_payload(rows, total))

    httpx_mock.add_callback(reply, url=API, is_reusable=True)

    jobs = JibeScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == total
    assert len({job.ats_id for job in jobs}) == total
    pages = sorted(
        int(parse_qs(urlparse(str(r.url)).query)["page"][0])
        for r in httpx_mock.get_requests()
    )
    assert pages == [1, 2, 3]


def test_walks_past_page_100_when_the_site_serves_it(httpx_mock) -> None:
    total = 100 * PAGE_SIZE + 5

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        first = (page - 1) * PAGE_SIZE
        rows = [_posting(str(i)) for i in range(first, min(first + PAGE_SIZE, total))]
        return httpx.Response(200, json=_payload(rows, total))

    httpx_mock.add_callback(reply, url=API, is_reusable=True)

    jobs = JibeScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == total
    assert len(httpx_mock.get_requests()) == 101


def _site(httpx_mock, postings: list[dict], *, served: int) -> list[dict[str, str]]:
    """A career site over ``postings`` that stops paging after ``served``
    rows a query and repeats its last page from there on.

    It filters on ``state`` and ``categories``, sorts by title from either
    end, and announces the states and categories of each query. Returns
    the queries asked.
    """
    asked: list[dict[str, str]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        query = {key: values[0] for key, values in parse_qs(request.url.query.decode()).items()}
        asked.append(query)
        pool = [posting["data"] for posting in postings]
        if "state" in query:
            pool = [data for data in pool if data["state"] == query["state"]]
        if "categories" in query:
            pool = [
                data for data in pool
                if query["categories"] in {entry["name"] for entry in data["categories"]}
            ]
        if query.get("sortBy") == "title":
            pool.sort(key=lambda data: data["title"], reverse="descending" in query)
        page = min(int(query["page"]), served // PAGE_SIZE)
        rows = pool[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        states = Counter(data["state"] for data in pool if data["state"])
        categories = Counter(entry["name"] for data in pool for entry in data["categories"])
        facets = {
            "facetList": {"state": [{"term": t, "count": n} for t, n in states.items()]},
            "categories": {"all": [{"category": c, "numJobs": n} for c, n in categories.items()]},
        }
        return httpx.Response(
            200, json=_payload([{"data": data} for data in rows], len(pool), filter=facets),
        )

    httpx_mock.add_callback(reply, url=API, is_reusable=True)
    return asked


def test_query_cut_short_is_read_from_both_ends_of_the_title_order(httpx_mock) -> None:
    postings = [_posting(str(i), state="", categories=[]) for i in range(450)]
    asked = _site(httpx_mock, postings, served=3 * PAGE_SIZE)

    jobs = JibeScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == 450
    orders = [(query.get("sortBy"), "descending" in query, query["page"]) for query in asked]
    # Each walk stops at the first page that repeats the one before it.
    assert orders[:8] == [
        *((None, False, str(page)) for page in range(1, 5)),
        *(("title", False, str(page)) for page in range(1, 5)),
    ]
    assert ("title", True, "1") in orders
    assert not any("state" in query or "categories" in query for query in asked)


def test_query_past_twice_the_cap_is_split_by_state_then_category(httpx_mock) -> None:
    """``Big`` is itself past twice the cap, so it is split by category.
    One posting has no state and is only reachable through its category."""
    postings = [
        *(_posting(f"s{i}", state="Small", categories=[{"name": "A"}]) for i in range(150)),
        *(_posting(f"a{i}", state="Big", categories=[{"name": "A"}]) for i in range(250)),
        *(_posting(f"b{i}", state="Big", categories=[{"name": "B"}]) for i in range(200)),
        _posting("x1", state="", categories=[{"name": "B"}]),
    ]
    asked = _site(httpx_mock, postings, served=2 * PAGE_SIZE)

    jobs = JibeScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == 601
    queries = {(query.get("state"), query.get("categories")) for query in asked}
    assert {("Small", None), ("Big", None), ("Big", "A"), ("Big", "B"), (None, "B")} <= queries
    assert ("Small", "A") not in queries


def test_capped_query_that_no_split_completes_fails_closed(httpx_mock) -> None:
    postings = [_posting(str(i), state="", categories=[]) for i in range(1000)]
    _site(httpx_mock, postings, served=3 * PAGE_SIZE)

    with pytest.raises(ScraperError, match="returned 300 of 1000 announced rows"):
        JibeScraper(HOST, include_descriptions=False).fetch()


def test_translated_twin_does_not_replace_english_row(httpx_mock) -> None:
    rows = [
        _posting("7", language="es-mx", title="Cajero"),
        _posting("7", language="en-us", title="Cashier"),
        _posting("7", language="fr-ca", title="Caissier"),
    ]
    httpx_mock.add_response(
        url=API,
        json=_payload(
            rows,
            3,
            languageCounts={
                "en-us": {"count": 1},
                "es-mx": {"count": 1},
                "fr-ca": {"count": 1},
            },
        ),
    )

    jobs = JibeScraper(HOST).fetch()

    assert [(job.title, job.language) for job in jobs] == [("Cashier", "en")]


def test_repeated_requisitions_count_as_rows(httpx_mock) -> None:
    """The same requisition listed for several locations is one posting."""
    rows = [_posting("7", city=f"City {i}") for i in range(10)]
    httpx_mock.add_response(url=API, json=_payload(rows, 10))

    jobs = JibeScraper(HOST).fetch()

    assert [job.ats_id for job in jobs] == ["careers.example.com:7"]


@pytest.mark.parametrize(
    ("apply_url", "expected"),
    [
        ("/jobs/7/apply", "https://careers.example.com/jobs/7/apply"),
        ("javascript:void(0)", None),
        ("", None),
    ],
)
def test_apply_url_is_absolute_or_dropped(httpx_mock, apply_url, expected) -> None:
    httpx_mock.add_response(url=API, json=_payload([_posting("7", apply_url=apply_url)], 1))

    job = JibeScraper(HOST).fetch()[0]

    assert (str(job.apply_url) if job.apply_url else None) == expected


def test_fails_closed_on_a_short_walk(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_payload([_posting("1")], 50))

    with pytest.raises(ScraperError, match="returned 1 of 50 announced rows"):
        JibeScraper(HOST).fetch()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"totalCount": 1},
        {"jobs": {}, "totalCount": 1},
        {"jobs": [], "totalCount": "3"},
        {"jobs": [], "totalCount": True},
        {"jobs": [{"nodata": 1}], "totalCount": 1},
    ],
)
def test_rejects_unexpected_payloads(httpx_mock, payload) -> None:
    httpx_mock.add_response(url=API, json=payload)

    with pytest.raises(ScraperError):
        JibeScraper(HOST).fetch()


@pytest.mark.parametrize("target", [
    "https://www.example.com/careers/",
    "https://careers.example.com/en/sites/CX_1/jobs",
])
def test_site_that_left_jibe_is_not_found(httpx_mock, target) -> None:
    httpx_mock.add_response(url=API, status_code=301, headers={"Location": target})
    httpx_mock.add_response(url=target, text="<!DOCTYPE html><html>Careers</html>")

    with pytest.raises(CompanyNotFoundError, match="no longer runs on Jibe"):
        JibeScraper(HOST).fetch()


def test_html_served_on_the_api_path_stays_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=API, text="<html>Maintenance</html>")

    with pytest.raises(MalformedJSONError):
        JibeScraper(HOST).fetch()


def test_postings_without_slug_or_title_are_skipped(httpx_mock) -> None:
    rows = [
        _posting("1"),
        _posting("", req_id=""),
        _posting("3", title="  "),
    ]
    httpx_mock.add_response(url=API, json=_payload(rows, 1))

    jobs = JibeScraper(HOST).fetch()

    assert [job.ats_id for job in jobs] == ["careers.example.com:1"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("careers.example.com", "careers.example.com"),
        ("https://Careers.Example.com/jobs?page=2", "careers.example.com"),
        ("careers.example.com/jobs", "careers.example.com"),
    ],
)
def test_normalize_host(value: str, expected: str) -> None:
    assert _normalize_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost",
        "http://careers.example.com",
        "https://user@careers.example.com",
        "https://careers.example.com:8443",
        "careers_example.com",
        "https://",
    ],
)
def test_normalize_host_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_host(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-28T21:27:00+0000", datetime(2026, 7, 28, 21, 27, tzinfo=UTC)),
        ("2026-07-28T23:27:00+0200", datetime(2026, 7, 28, 21, 27, tzinfo=UTC)),
        ("2026-07-28T21:27:00Z", datetime(2026, 7, 28, 21, 27, tzinfo=UTC)),
        ("2026-07-28T21:27:00", datetime(2026, 7, 28, 21, 27, tzinfo=UTC)),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_datetime(value, expected) -> None:
    assert _parse_datetime(value) == expected


def test_rows_that_do_not_parse_fail_the_coverage_check(httpx_mock) -> None:
    rows = [_posting("", req_id="") for _ in range(20)]
    httpx_mock.add_response(url=API, json=_payload(rows, 20))

    with pytest.raises(ScraperError, match="returned 0 of 20 announced rows"):
        JibeScraper(HOST).fetch()


@pytest.mark.parametrize(("returned", "raises"), [(9, True), (10, False)])
def test_coverage_floor_rounds_up(httpx_mock, returned: int, raises: bool) -> None:
    """11 announced rows need 10 (90% rounded up), not 9."""
    rows = [_posting(str(i)) for i in range(returned)]
    httpx_mock.add_response(url=API, json=_payload(rows, 11))

    if raises:
        with pytest.raises(ScraperError, match="returned 9 of 11"):
            JibeScraper(HOST).fetch()
    else:
        assert len(JibeScraper(HOST).fetch()) == 10


def test_capped_board_with_repeated_locations_is_complete(httpx_mock) -> None:
    """Past the cap, a requisition listed in two cities is two rows, one posting."""
    postings = [
        _posting(str(i), state=state, city=state, full_location=state, location_name=state)
        for state in ("East", "West")
        for i in range(150)
    ]
    _site(httpx_mock, postings, served=PAGE_SIZE)

    jobs = JibeScraper(HOST, include_descriptions=False).fetch()

    assert len(jobs) == 150


def test_states_from_locations_sums_cities() -> None:
    payload = {
        "filter": {
            "locations": {
                "all": [
                    {"city": "Abilene", "state": "Texas", "count": 9},
                    {"city": "Austin", "state": "Texas", "count": 4},
                    {"city": "Acworth", "state": "Georgia", "count": 8},
                    {"city": "Nowhere", "state": "", "count": 3},
                    {"city": "Bad", "state": "Ohio", "count": "2"},
                ],
            },
        },
    }

    assert _states_from_locations(payload) == [("Georgia", 8), ("Texas", 13)]
    assert _states_from_locations({}) == []
