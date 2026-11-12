"""Tests for the Jibe (iCIMS career site) scraper."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.scrapers.jibe import (
    PAGE_SIZE,
    QUERY_CAP,
    JibeScraper,
    _expected_unique,
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


def test_capped_board_is_split_by_state_then_category(httpx_mock) -> None:
    """A board above the 10,000 cap is walked state by state.

    ``Big`` is itself above the cap, so it is split by category. One
    posting has no state and is only reachable through its category.
    """
    total = QUERY_CAP + 300
    small = [_posting(f"s{i}", state="Small") for i in range(150)]
    big_a = [_posting(f"a{i}", state="Big", categories=[{"name": "A"}]) for i in range(120)]
    big_b = [_posting(f"b{i}", state="Big", categories=[{"name": "B"}]) for i in range(30)]
    stateless = [_posting("x1", state="", categories=[{"name": "B"}])]
    filters = {
        "filter": {
            "facetList": {
                "state": [
                    {"term": "Small", "count": 150},
                    {"term": "Big", "count": QUERY_CAP + 149},
                ],
            },
            "categories": {
                "all": [
                    {"category": "A", "numJobs": 120},
                    {"category": "B", "numJobs": 31},
                ],
            },
        },
    }
    seen: list[tuple[str | None, str | None, int]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        state = query.get("state", [None])[0]
        category = query.get("categories", [None])[0]
        page = int(query["page"][0])
        seen.append((state, category, page))
        if state == "Small":
            pool = small
        elif state == "Big":
            pool = big_a if category == "A" else big_b
        elif category == "A":
            pool = big_a
        elif category == "B":
            pool = big_b + stateless
        else:
            pool = small[:1]
        rows = pool[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return httpx.Response(200, json=_payload(rows, total, **filters))

    httpx_mock.add_callback(reply, url=API, is_reusable=True)
    scraper = JibeScraper(HOST, include_descriptions=False)
    # The fixture holds 301 postings, far below the announced total.
    with pytest.raises(ScraperError, match="returned 301 of"):
        scraper.fetch()

    assert ("Small", None, 1) in seen and ("Small", None, 2) in seen
    assert ("Big", "A", 1) in seen and ("Big", "A", 2) in seen
    assert ("Big", "B", 1) in seen
    assert ("Big", None, 1) not in seen
    assert (None, "B", 1) in seen


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


def test_expected_unique_uses_the_largest_language() -> None:
    payload = {"languageCounts": {"en-us": {"count": 90}, "es-mx": {"count": 10}}}

    assert _expected_unique(payload, 100) == 90
    assert _expected_unique({}, 100) == 100
    assert _expected_unique({"languageCounts": {"en-us": {"count": True}}}, 100) == 100


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
