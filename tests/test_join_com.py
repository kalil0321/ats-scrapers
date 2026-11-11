"""join.com: slug -> company id, then the GraphQL publicJobs listing."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.scrapers import JoinComScraper

GRAPHQL = "https://join.com/candidate-api/graphql"


def _company_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://join.com/companies/acme",
        text=(
            '<html><script>{"company":{"id":42,"name":"Acme GmbH",'
            '"domain":"acme"}}</script></html>'
        ),
    )


def _listing(items: list, *, page_count: int = 1, row_count: int | None = None) -> dict:
    return {
        "data": {
            "publicJobs": {
                "items": items,
                "pageInfo": {
                    "pageCount": page_count,
                    "rowCount": len(items) if row_count is None else row_count,
                },
            }
        }
    }


def _item(job_id: int, title: str = "Designer") -> dict:
    return {
        "id": job_id,
        "idParam": f"{job_id}-designer",
        "title": title,
        "createdAt": "2026-10-01T09:00:00.000Z",
        "workplaceType": "HYBRID",
        "remoteType": None,
        "city": {"cityName": "Berlin", "countryName": "Germany"},
        "category": {"name": "Design"},
        "employmentType": {"name": "Employee"},
    }


def _scraper() -> JoinComScraper:
    return JoinComScraper("acme", include_descriptions=False)


def _graphql_inputs(httpx_mock) -> list[dict]:
    return [
        json.loads(r.content)["variables"]["input"]
        for r in httpx_mock.get_requests(method="POST", url=GRAPHQL)
    ]


def test_join_com_lists_jobs_via_graphql(httpx_mock) -> None:
    _company_page(httpx_mock)
    httpx_mock.add_response(
        method="POST", url=GRAPHQL, json=_listing([_item(100), None], row_count=1)
    )

    jobs = _scraper().fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Designer"
    assert job.ats_id == "100"
    assert str(job.url) == "https://join.com/companies/acme/jobs/100-designer"
    assert job.location == "Berlin, Germany"
    assert job.commitment == "Employee"
    assert job.posted_at == datetime(2026, 10, 1, 9, tzinfo=UTC)
    assert job.raw == {
        "category": {"name": "Design"},
        "employmentType": {"name": "Employee"},
        "workplaceType": "HYBRID",
    }
    [request] = httpx_mock.get_requests(method="POST", url=GRAPHQL)
    assert request.headers["content-language"] == "en-us"
    assert _graphql_inputs(httpx_mock) == [
        {
            "companyId": 42,
            "paginationPage": {"page": 1, "pageSize": 100},
            "sort": [{"name": "title", "direction": "ASC"}],
        }
    ]


def test_join_com_follows_page_count_and_dedupes(httpx_mock) -> None:
    _company_page(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=GRAPHQL,
        json=_listing([_item(1), _item(2)], page_count=2, row_count=3),
    )
    httpx_mock.add_response(
        method="POST",
        url=GRAPHQL,
        json=_listing([_item(2), _item(3)], page_count=2, row_count=3),
    )

    jobs = _scraper().fetch()

    assert [j.ats_id for j in jobs] == ["1", "2", "3"]
    assert [i["paginationPage"]["page"] for i in _graphql_inputs(httpx_mock)] == [1, 2]


def test_join_com_company_without_jobs_returns_empty(httpx_mock) -> None:
    _company_page(httpx_mock)
    httpx_mock.add_response(
        method="POST", url=GRAPHQL, json=_listing([], page_count=0)
    )
    assert _scraper().fetch() == []


def test_join_com_raises_when_row_count_has_no_items(httpx_mock) -> None:
    _company_page(httpx_mock)
    httpx_mock.add_response(
        method="POST", url=GRAPHQL, json=_listing([], page_count=1, row_count=4)
    )
    with pytest.raises(ScraperError, match="reported 4 jobs but returned none"):
        _scraper().fetch()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (
            200,
            {
                "errors": [{"message": "Can not create a field mask."}],
                "data": {"publicJobs": None},
            },
        ),
        (200, {"data": None}),
        (200, {"data": {"publicJobs": {"items": []}}}),
        (200, {"data": {"publicJobs": {"pageInfo": {"pageCount": 1, "rowCount": 0}}}}),
        (200, [{"param": "pageSize", "msg": "Invalid value"}]),
        (200, _listing([{"title": "No id"}])),
        (400, {"errors": [{"message": "Field \"companyId\" was not provided."}]}),
    ],
    ids=[
        "graphql-errors",
        "null-data",
        "no-page-info",
        "no-items",
        "rest-error-shape",
        "item-without-id",
        "http-400",
    ],
)
def test_join_com_unreadable_listing_raises(httpx_mock, status, body) -> None:
    _company_page(httpx_mock)
    httpx_mock.add_response(method="POST", url=GRAPHQL, status_code=status, json=body)
    with pytest.raises(ScraperError):
        _scraper().fetch()


def test_join_com_resolves_id_without_domain_field(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://join.com/companies/acme",
        text='<html><script>{"company":{"id":"42","slug":"acme"}}</script></html>',
    )
    httpx_mock.add_response(method="POST", url=GRAPHQL, json=_listing([_item(100)]))
    assert [j.ats_id for j in _scraper().fetch()] == ["100"]
    assert _graphql_inputs(httpx_mock)[0]["companyId"] == 42


def test_join_com_404(httpx_mock) -> None:
    httpx_mock.add_response(url="https://join.com/companies/missing", status_code=404)
    with pytest.raises(CompanyNotFoundError):
        JoinComScraper("missing").fetch()
