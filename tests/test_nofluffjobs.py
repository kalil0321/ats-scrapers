"""Tests for the NoFluffJobs scraper.

NoFluffJobs ships a POST search endpoint that returns one page at a
time. Pin parsing of the rich payload (places + country → alpha-2,
salary range w/ currency + period + contract type, posted epoch-ms,
seniority / category / requirement tiles → raw) plus the pagination
loop driven by ``totalPages``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import NoFluffJobsScraper, ScraperRegistry

API_URL = "https://nofluffjobs.com/api/search/posting"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.nofluffjobs as nfj
    monkeypatch.setattr(nfj, "MAX_RETRIES", 1)
    monkeypatch.setattr(nfj, "RETRY_BASE_DELAY", 0.0)


def _posting(
    *,
    job_id: str = "senior-angular-developer-acme-Kraków-1",
    title: str = "Senior Angular Developer",
    company: str = "Acme",
    url_slug: str = "senior-angular-developer-acme-krakow",
    places: list[dict[str, Any]] | None = None,
    fully_remote: bool = False,
    salary_from: float | None = 21840.0,
    salary_to: float | None = 28560.0,
    salary_currency: str | None = "PLN",
    salary_type: str = "b2b",
    seniority: list[str] | None = None,
    category: str = "frontend",
    technology: str = "Angular",
    posted: int = 1777521835788,
    reference: str = "S6VTM4BA",
    tiles: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if places is None:
        places = [
            {
                "country": {"code": "POL", "name": "Poland"},
                "city": "Kraków",
            }
        ]
    if seniority is None:
        seniority = ["Senior"]
    if tiles is None:
        tiles = [
            {"value": "frontend", "type": "category"},
            {"value": "Angular", "type": "requirement"},
            {"value": "TypeScript", "type": "requirement"},
        ]
    salary: dict[str, Any] = {"type": salary_type}
    if salary_from is not None:
        salary["from"] = salary_from
    if salary_to is not None:
        salary["to"] = salary_to
    if salary_currency is not None:
        salary["currency"] = salary_currency
    return {
        "id": job_id,
        "name": company,
        "title": title,
        "url": url_slug,
        "location": {
            "places": places,
            "fullyRemote": fully_remote,
        },
        "fullyRemote": fully_remote,
        "salary": salary,
        "seniority": seniority,
        "category": category,
        "technology": technology,
        "posted": posted,
        "regions": ["pl"],
        "reference": reference,
        "tiles": {"values": tiles},
    }


def _response(
    postings: list[dict[str, Any]],
    *,
    total_pages: int = 1,
    total_count: int | None = None,
) -> dict[str, Any]:
    return {
        "criteriaSearch": {},
        "postings": postings,
        "totalCount": total_count if total_count is not None else len(postings),
        "totalPages": total_pages,
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_nofluffjobs() -> None:
    assert ScraperRegistry.get(ATSType.NOFLUFFJOBS) is NoFluffJobsScraper


def test_ats_type_value() -> None:
    assert ATSType.NOFLUFFJOBS.value == "nofluffjobs"


# --- happy path -------------------------------------------------------------


def test_parses_full_posting(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting()]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.ats_type is ATSType.NOFLUFFJOBS
    assert j.ats_id == "senior-angular-developer-acme-Kraków-1"
    assert j.title == "Senior Angular Developer"
    assert j.company == "Acme"
    assert str(j.url) == (
        "https://nofluffjobs.com/job/senior-angular-developer-acme-krakow"
    )
    assert j.location == "Kraków, Poland"
    assert j.country_iso == "PL"
    assert j.language == "en"
    assert j.salary_currency == "PLN"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 21840.0
    assert j.salary_max == 28560.0
    assert j.employment_type == "CONTRACT"
    assert j.commitment == "b2b"
    assert j.requisition_id == "S6VTM4BA"
    assert j.posted_at == datetime.fromtimestamp(
        1777521835788 / 1000.0, tz=UTC
    ).replace(tzinfo=None)
    assert j.raw is not None
    assert j.raw["category"] == "frontend"
    assert j.raw["seniority"] == ["Senior"]
    assert j.raw["must_haves"] == ["Angular", "TypeScript"]
    assert j.raw["regions"] == ["pl"]
    assert j.raw["contract_type"] == "b2b"


def test_global_id_format() -> None:
    """``global_id`` follows ``nofluffjobs:{id}`` so consumers can
    split on the first colon."""
    from jobhive.models import Job

    job = Job(
        url="https://nofluffjobs.com/job/x",
        title="x",
        company="x",
        ats_type=ATSType.NOFLUFFJOBS,
        ats_id="senior-angular-acme-krakow-1",
    )
    assert job.global_id == "nofluffjobs:senior-angular-acme-krakow-1"


# --- employment-type mapping ------------------------------------------------


@pytest.mark.parametrize("contract, expected_emp", [
    ("permanent", "FULL_TIME"),
    ("b2b", "CONTRACT"),
    ("mandate", "CONTRACT"),
    ("freelance", "CONTRACT"),
    ("internship", "INTERN"),
    ("trainee", "INTERN"),
])
def test_employment_type_mapping(
    contract: str, expected_emp: str, httpx_mock
) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(job_id=f"x-{contract}", salary_type=contract)]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.employment_type == expected_emp
    assert j.commitment == contract


def test_unknown_contract_type_leaves_employment_type_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(job_id="x", salary_type="weird-contract")]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.employment_type is None
    assert j.commitment == "weird-contract"


# --- location + country -----------------------------------------------------


def test_remote_only_posting(httpx_mock) -> None:
    """Some postings have a single ``Remote`` place with no country
    object — set ``is_remote=True``, location='Remote', country_iso=None."""
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(
            job_id="remote-1",
            places=[{"city": "Remote"}],
            fully_remote=True,
        )]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.location == "Remote"
    assert j.country_iso is None
    assert j.is_remote is True


def test_multi_place_pipe_joined_and_first_country_wins(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(
            job_id="multi",
            places=[
                {"city": "Remote"},
                {
                    "country": {"code": "POL", "name": "Poland"},
                    "city": "Katowice",
                },
                {
                    "country": {"code": "DEU", "name": "Germany"},
                    "city": "Berlin",
                },
            ],
        )]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.location == "Remote | Katowice, Poland | Berlin, Germany"
    assert j.country_iso == "PL"  # first place w/ country code wins


@pytest.mark.parametrize("code3, code2", [
    ("POL", "PL"), ("DEU", "DE"), ("NLD", "NL"),
    ("CZE", "CZ"), ("GBR", "GB"), ("USA", "US"),
])
def test_alpha3_to_alpha2_country_mapping(
    code3: str, code2: str, httpx_mock
) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(
            job_id=f"x-{code3}",
            places=[{
                "country": {"code": code3, "name": "X"}, "city": "Y",
            }],
        )]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.country_iso == code2


def test_unknown_country_code_yields_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(
            job_id="x",
            places=[{"country": {"code": "XYZ", "name": "Atlantis"},
                     "city": "Sub"}],
        )]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.country_iso is None
    assert j.location == "Sub, Atlantis"


# --- salary -----------------------------------------------------------------


def test_no_salary_when_amounts_missing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(
            job_id="x", salary_from=None, salary_to=None, salary_currency=None,
        )]),
    )
    j = NoFluffJobsScraper("any").fetch()[0]
    assert j.salary_currency is None
    assert j.salary_period is None
    assert j.salary_min is None
    assert j.salary_max is None


def test_salary_period_from_payload_wins_over_query_default(
    httpx_mock,
) -> None:
    """When the posting's salary explicitly carries a ``period``, use
    it; fall back to the query-default only when missing."""
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "EUR", "salaryPeriod": "year",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([{
            **_posting(job_id="hourly", salary_from=50, salary_to=80),
            "salary": {
                "from": 50, "to": 80, "type": "b2b",
                "currency": "EUR", "period": "hour",
            },
        }]),
    )
    j = NoFluffJobsScraper(
        "any", salary_currency="EUR", salary_period="year"
    ).fetch()[0]
    assert j.salary_period == "HOUR"


# --- pagination -------------------------------------------------------------


def test_paginates_until_total_pages(httpx_mock) -> None:
    """``totalPages=3`` should drive three POSTs, with page=1..3."""
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(job_id="p1")], total_pages=3),
    )
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "2", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(job_id="p2")], total_pages=3),
    )
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "3", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(job_id="p3")], total_pages=3),
    )
    jobs = NoFluffJobsScraper("any").fetch()
    assert sorted(j.ats_id for j in jobs) == ["p1", "p2", "p3"]


def test_pagination_dedupes_repeated_ids_across_pages(httpx_mock) -> None:
    """If the server returns the same id on two adjacent pages
    (race / re-sort), the second appearance is dropped."""
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response(
            [_posting(job_id="a"), _posting(job_id="b")], total_pages=2,
        ),
    )
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "2", "pageSize": "200",
        }),
        method="POST",
        json=_response(
            [_posting(job_id="b"), _posting(job_id="c")], total_pages=2,
        ),
    )
    jobs = NoFluffJobsScraper("any").fetch()
    assert sorted(j.ats_id for j in jobs) == ["a", "b", "c"]


def test_pagination_short_circuits_on_empty_page(httpx_mock) -> None:
    """If a page comes back empty before totalPages, bail (don't spin)."""
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([_posting(job_id="a")], total_pages=10),
    )
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "2", "pageSize": "200",
        }),
        method="POST",
        json=_response([], total_pages=10),
    )
    jobs = NoFluffJobsScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["a"]


# --- defensive --------------------------------------------------------------


def test_skips_posting_missing_required_fields(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=_response([
            _posting(),
            {"id": "no-title", "name": "x", "url": "slug"},  # no title
            {"title": "x", "name": "y"},  # no id, no url
        ]),
    )
    jobs = NoFluffJobsScraper("any").fetch()
    assert len(jobs) == 1


def test_non_dict_response_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json=[1, 2, 3],
    )
    with pytest.raises(ScraperError, match="API shape changed"):
        NoFluffJobsScraper("any").fetch()


def test_postings_not_a_list_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        json={"postings": "nope", "totalPages": 1},
    )
    with pytest.raises(ScraperError, match="'postings' is"):
        NoFluffJobsScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(API_URL).copy_merge_params({
            "salaryCurrency": "PLN", "salaryPeriod": "month",
            "page": "1", "pageSize": "200",
        }),
        method="POST",
        status_code=500,
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        NoFluffJobsScraper("any").fetch()
