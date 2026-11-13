"""Tests for the ClearCompany career-site scraper."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx
import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ClearCompanyScraper, ScraperRegistry
from ats_scrapers.scrapers import clearcompany as module
from ats_scrapers.scrapers.clearcompany import _amount, _apply_url, _parse_date, _parse_slug

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py.

SITE = "https://apply.clearcompany.com/acme"
SITE_ID = "41faeaee-3ddd-83bd-84dc-37aa557a1e29"
API = f"https://careers-api.clearcompany.com/v1/{SITE_ID}"


def _site_page(site_id: str = SITE_ID) -> str:
    return (
        "<html><head><title>Acme &#x2013; Careers</title></head><body>"
        '<div id="cc-widget"><script '
        'src="https://careers-content.clearcompany.com/js/v1/career-site.js?hosted=true" '
        f'data-site-id="{site_id}" data-default-source="CJB-0"></script></div>'
        "</body></html>"
    )


def _job_id(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def _row(n: int, title: str = "Welder", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": _job_id(n),
        "userDefinedId": f"R{n}",
        "positionTitle": title,
        "description": "<p>Weld &amp; fit.</p>",
        "openDate": "2026-11-10T00:00:00",
        "postedDate": "2026-11-12T00:00:00",
        "departmentName": "Plant",
        "officeName": "Mooresville",
        "jobFunctionName": None,
        "location": "MOORESVILLE NC",
        "locations": [
            {
                "city": "Mooresville",
                "subdivision": "NC",
                "country": "US",
                "isRemote": False,
                "isNationwide": False,
            }
        ],
        "brandName": "Acme Foods",
        "applyLink": f"https://acme.clearcompany.com/careers/jobs/{_job_id(n)}/apply",
        "salaryRangeLow": "0",
        "salaryRangeHigh": "0",
        "salaryType": None,
        "customFieldAnswers": [],
    }
    row.update(overrides)
    return row


def _serve(httpx_mock, rows: list[dict[str, object]], total: int | None = None) -> None:
    """Answer the listing the way the live API does: the page asked for,
    in pages of ``pageSize``, and no result past the end."""
    count = len(rows) if total is None else total

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pageIndex"])
        size = int(request.url.params["pageSize"])
        chunk = rows[page * size : (page + 1) * size]
        return httpx.Response(
            200,
            json={
                "results": chunk,
                "currentPageIndex": page,
                "totalCount": count,
                "currentPageCount": len(chunk),
            },
        )

    httpx_mock.add_callback(
        reply, url=re.compile(rf"^{re.escape(API)}\?"), is_reusable=True
    )


def _pages(httpx_mock) -> list[int]:
    return [
        int(request.url.params["pageIndex"])
        for request in httpx_mock.get_requests()
        if request.url.host == "careers-api.clearcompany.com"
    ]


# --- Registry and slug ------------------------------------------------------


def test_registry_resolves_clearcompany() -> None:
    assert ScraperRegistry.get(ATSType.CLEARCOMPANY) is ClearCompanyScraper


@pytest.mark.parametrize(
    "slug",
    ["acme", " acme ", "https://apply.clearcompany.com/acme", "https://apply.clearcompany.com/acme/"],
)
def test_slug_forms(slug: str) -> None:
    assert _parse_slug(slug) == "acme"


def test_slug_keeps_its_case_and_dashes() -> None:
    assert _parse_slug("https://APPLY.clearcompany.com/PeopleInc-FR?jobId=1") == "PeopleInc-FR"


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "   ",
        "-acme",
        "acme/jobs",
        "acme corp",
        "ac\nme",
        "http://apply.clearcompany.com/acme",
        "https://apply.clearcompany.com/",
        "https://apply.clearcompany.com/acme/extra",
        "https://apply.clearcompany.com/..%2Fadmin",
        "https://acme.clearcompany.com/careers",
        "https://apply.clearcompany.com.evil.com/acme",
        "https://user:pw@apply.clearcompany.com/acme",
        "https://apply.clearcompany.com:8443/acme",
    ],
)
def test_invalid_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(ScraperError):
        ClearCompanyScraper(slug)


# --- Listing ----------------------------------------------------------------


def test_parses_a_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(1, "Plant Manager"), _row(2, "Welder &amp; Fitter")])

    jobs = ClearCompanyScraper("acme", company_name="Acme Group").fetch()

    assert [job.title for job in jobs] == ["Plant Manager", "Welder & Fitter"]
    first = jobs[0]
    # The posting names its own employer; the row's name is the fallback.
    assert first.company == "Acme Foods"
    assert first.ats_type is ATSType.CLEARCOMPANY
    assert first.ats_id == _job_id(1)
    assert str(first.url) == f"{SITE}?jobId={_job_id(1)}"
    assert str(first.apply_url) == f"https://acme.clearcompany.com/careers/jobs/{_job_id(1)}/apply"
    assert first.location == "Mooresville, NC, US"
    assert first.country_iso == "US"
    assert first.is_remote is False
    assert first.department == "Plant"
    assert first.team is None
    assert first.requisition_id == "R1"
    assert first.posted_at == datetime(2026, 11, 12, tzinfo=UTC)
    assert first.description == "<p>Weld & fit.</p>"
    assert first.salary_min is None and first.salary_max is None
    assert first.salary_period is None
    assert first.raw == {"officeName": "Mooresville"}


def test_site_id_is_read_from_the_career_site_and_used_for_the_api(httpx_mock) -> None:
    other = "0f463492-2cf5-4db3-8ea2-2071523b2f9f"
    httpx_mock.add_response(url=SITE, text=_site_page(other.upper()))
    httpx_mock.add_response(
        url=f"https://careers-api.clearcompany.com/v1/{other}?pageIndex=0&pageSize=100",
        json={"results": [], "currentPageIndex": 0, "totalCount": 0, "currentPageCount": 0},
    )

    assert ClearCompanyScraper("acme").fetch() == []
    api = next(
        r for r in httpx_mock.get_requests() if r.url.host == "careers-api.clearcompany.com"
    )
    assert api.headers["Accept"] == "application/json"


def test_company_falls_back_to_the_row_name_then_the_site(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page(), is_reusable=True)
    _serve(httpx_mock, [_row(1, brandName=None)])

    assert ClearCompanyScraper("acme", company_name="Acme Group").fetch()[0].company == "Acme Group"
    assert ClearCompanyScraper("acme").fetch()[0].company == "acme"


def test_job_id_is_lowercased(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(1, id=_job_id(0xABCDEF).upper())])

    job = ClearCompanyScraper("acme").fetch()[0]

    assert job.ats_id == _job_id(0xABCDEF)
    assert str(job.url) == f"{SITE}?jobId={_job_id(0xABCDEF)}"


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(1)])

    assert ClearCompanyScraper("acme", include_descriptions=False).fetch()[0].description is None


def test_several_locations_remote_and_salary(httpx_mock) -> None:
    row = _row(
        1,
        locations=[
            {"city": "Austin", "subdivision": "TX", "country": "US", "isRemote": True},
            {"city": "Toronto", "subdivision": "Ontario", "country": "CA", "isRemote": False},
            {"city": "Austin", "subdivision": "TX", "country": "US", "isRemote": True},
            "not a place",
        ],
        salaryRangeLow="15.75",
        salaryRangeHigh="24",
        salaryType="Per Hour",
        jobFunctionName="Marketing",
    )
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [row])

    job = ClearCompanyScraper("acme").fetch()[0]

    assert job.location == "Austin, TX, US; Toronto, Ontario, CA"
    assert job.country_iso is None  # two countries
    assert job.is_remote is True
    assert (job.salary_min, job.salary_max, job.salary_period) == (15.75, 24.0, "HOUR")
    assert job.team == "Marketing"
    assert job.raw == {"officeName": "Mooresville", "salaryType": "Per Hour"}


def test_row_without_locations_uses_the_location_line(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(1, locations=None, location=" REMOTE ", salaryType="Per Year")])

    job = ClearCompanyScraper("acme").fetch()[0]

    assert job.location == "REMOTE"
    assert job.is_remote is None
    assert job.country_iso is None
    # A salary type without an amount is not a salary.
    assert job.salary_period is None


def test_apply_link_on_another_host_is_dropped(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(1, applyLink="https://evil.example.com/apply")])

    assert ClearCompanyScraper("acme").fetch()[0].apply_url is None


# --- Paging -----------------------------------------------------------------


def test_reads_every_page(httpx_mock) -> None:
    rows = [_row(n, f"Job {n}") for n in range(230)]
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, rows)

    jobs = ClearCompanyScraper("acme").fetch()

    assert [job.requisition_id for job in jobs] == [f"R{n}" for n in range(230)]
    assert _pages(httpx_mock) == [0, 1, 2]


def test_single_short_page_is_one_request(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(1), _row(2)])

    assert len(ClearCompanyScraper("acme").fetch()) == 2
    assert _pages(httpx_mock) == [0]


def test_full_last_page_is_followed_by_an_empty_one(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(n) for n in range(200)])

    assert len(ClearCompanyScraper("acme").fetch()) == 200
    assert _pages(httpx_mock) == [0, 1, 2]


def test_short_page_before_the_announced_total_does_not_end_the_walk(httpx_mock) -> None:
    """The total says more: the walk reads on to an empty page, and the
    shortfall is then a failure, not a short list."""
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(n) for n in range(40)], total=400)

    with pytest.raises(ScraperError, match="announces 400 postings and listed 40"):
        ClearCompanyScraper("acme").fetch()
    assert _pages(httpx_mock) == [0, 1]


def test_small_gap_to_the_total_is_tolerated(httpx_mock) -> None:
    """A posting can close during the walk."""
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(n) for n in range(95)], total=100)

    assert len(ClearCompanyScraper("acme").fetch()) == 95


def test_posting_repeated_across_pages_is_kept_once(httpx_mock) -> None:
    rows = [_row(n) for n in range(100)] + [_row(99), _row(100)]
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, rows, total=101)

    jobs = ClearCompanyScraper("acme").fetch()

    assert len(jobs) == 101
    assert len({job.ats_id for job in jobs}) == 101


def test_rows_that_do_not_parse_count_against_coverage(httpx_mock) -> None:
    rows = [_row(n) for n in range(8)] + [_row(8, positionTitle=""), _row(9, id="not-a-guid")]
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, rows)

    with pytest.raises(ScraperError, match="could read only 8 of the 10 postings listed"):
        ClearCompanyScraper("acme").fetch()


def test_page_guard_raises_instead_of_returning_a_partial_list(monkeypatch, httpx_mock) -> None:
    monkeypatch.setattr(module, "MAX_PAGES", 2)
    httpx_mock.add_response(url=SITE, text=_site_page())
    _serve(httpx_mock, [_row(n) for n in range(250)])

    with pytest.raises(ScraperError, match="after 2 pages"):
        ClearCompanyScraper("acme").fetch()
    assert _pages(httpx_mock) == [0, 1]


def test_failed_page_fails_the_whole_walk(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch

    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 2)
    httpx_mock.add_response(url=SITE, text=_site_page())
    httpx_mock.add_response(
        url=f"{API}?pageIndex=0&pageSize=100",
        json={
            "results": [_row(n) for n in range(100)],
            "currentPageIndex": 0,
            "totalCount": 150,
            "currentPageCount": 100,
        },
    )
    httpx_mock.add_response(url=f"{API}?pageIndex=1&pageSize=100", status_code=502, is_reusable=True)

    with pytest.raises(ScraperError, match="502"):
        ClearCompanyScraper("acme").fetch()


# --- Unexpected responses ---------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"totalCount": 1},
        {"results": [], "totalCount": None},
        {"results": [], "totalCount": "many"},
        {"results": [], "totalCount": -1},
        {"results": [], "totalCount": True},
        {"results": {"a": 1}, "totalCount": 1},
        {"results": ["row"], "totalCount": 1},
        # Another page than the one asked for.
        {"results": [], "totalCount": 0, "currentPageIndex": 3},
    ],
)
def test_unexpected_listing_raises(httpx_mock, payload) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    httpx_mock.add_response(url=f"{API}?pageIndex=0&pageSize=100", json=payload)

    with pytest.raises(ScraperError):
        ClearCompanyScraper("acme").fetch()


def test_counts_given_as_digits_are_read(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    httpx_mock.add_response(
        url=f"{API}?pageIndex=0&pageSize=100",
        json={"results": [_row(1)], "currentPageIndex": "0", "totalCount": "1"},
    )

    assert len(ClearCompanyScraper("acme").fetch()) == 1


def test_missing_career_site_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SITE,
        status_code=404,
        text="<h1 class='cc-offline-title'>This careers page isn't available right now</h1>",
    )

    with pytest.raises(CompanyNotFoundError):
        ClearCompanyScraper("acme").fetch()


def test_career_site_without_a_site_id_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text="<html><title>Acme</title></html>")

    with pytest.raises(ScraperError, match="names no site id") as excinfo:
        ClearCompanyScraper("acme").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_unknown_site_id_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=SITE, text=_site_page())
    httpx_mock.add_response(url=f"{API}?pageIndex=0&pageSize=100", status_code=404)

    with pytest.raises(CompanyNotFoundError):
        ClearCompanyScraper("acme").fetch()


# --- Small helpers ----------------------------------------------------------


def test_amount() -> None:
    assert _amount("15.75") == 15.75
    assert _amount(80000) == 80000.0
    assert _amount("0") is None
    assert _amount(0) is None
    assert _amount("-5") is None
    assert _amount("") is None
    assert _amount(None) is None
    assert _amount(True) is None
    assert _amount("lots") is None
    assert _amount("inf") is None
    assert _amount("nan") is None


def test_apply_url() -> None:
    link = "https://acme.clearcompany.com/careers/jobs/1/apply"
    assert _apply_url(link) == link
    assert _apply_url(f" {link} ") == link
    assert _apply_url("http://acme.clearcompany.com/careers/jobs/1/apply") is None
    assert _apply_url("https://clearcompany.com.evil.com/apply") is None
    assert _apply_url("https://acme.clearcompany.com.evil.com/apply") is None
    assert _apply_url(None) is None
    assert _apply_url(json.dumps({"a": 1})) is None


def test_parse_date() -> None:
    assert _parse_date("2026-11-12T00:00:00") == datetime(2026, 11, 12, tzinfo=UTC)
    assert _parse_date("2026-11-12T08:30:00Z") == datetime(2026, 11, 12, 8, 30, tzinfo=UTC)
    assert _parse_date("yesterday") is None
    assert _parse_date("") is None
    assert _parse_date(None) is None
