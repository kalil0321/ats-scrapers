"""Guardrails for pathological Workday tenants."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.scrapers.workday import WorkdayScraper

URL = "https://acme.wd5.myworkdayjobs.com/External"
API = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"


def test_workday_deadline_raises() -> None:
    scraper = WorkdayScraper(
        "https://accenture.wd103.myworkdayjobs.com/accenturecareers",
        max_fetch_seconds=1,
    )
    scraper._deadline = time.monotonic() - 1

    with pytest.raises(ScraperError, match="max_fetch_seconds"):
        scraper._check_deadline()


def test_workday_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    class FakeClient:
        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(
                status_code=429,
                headers={"Retry-After": "3600"},
            )

    monkeypatch.setattr("ats_scrapers.scrapers.workday.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("ats_scrapers.scrapers.workday.MAX_RETRY_DELAY", 7.0)

    scraper = WorkdayScraper(
        "https://accenture.wd103.myworkdayjobs.com/accenturecareers",
    )

    async def run() -> None:
        with pytest.raises(ScraperError, match=r"gave up .*: HTTP 429"):
            await scraper._request(
                FakeClient(),
                "https://example.com/wday/cxs/accenture/site/jobs",
                asyncio.Semaphore(1),
                applied_facets={},
                offset=0,
            )

    asyncio.run(run())

    assert delays == [7.0, 7.0, 7.0]


def test_workday_retries_html_outage_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    class FakeResponse:
        def __init__(self, payload: dict[str, object] | None) -> None:
            self.status_code = 200
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.payload = payload

        def json(self) -> dict[str, object]:
            if self.payload is None:
                raise ValueError("outage page is HTML")
            return self.payload

    class FakeClient:
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return FakeResponse(None)
            return FakeResponse({"jobPostings": [], "total": 0})

    monkeypatch.setattr("ats_scrapers.scrapers.workday.asyncio.sleep", fake_sleep)

    scraper = WorkdayScraper(
        "https://2020companies.wd1.myworkdayjobs.com/external_careers",
    )

    async def run() -> None:
        payload = await scraper._request(
            FakeClient(),
            "https://example.com/wday/cxs/2020companies/site/jobs",
            asyncio.Semaphore(1),
            applied_facets={},
            offset=0,
        )
        assert payload == {"jobPostings": [], "total": 0}

    asyncio.run(run())

    assert calls == 2
    assert delays == [1.0]


def test_workday_persistent_html_outage_raises_scraper_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def json(self) -> dict[str, object]:
            raise ValueError("outage page is HTML")

    class FakeClient:
        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("ats_scrapers.scrapers.workday.asyncio.sleep", fake_sleep)

    scraper = WorkdayScraper(
        "https://2020companies.wd1.myworkdayjobs.com/external_careers",
    )

    async def run() -> None:
        with pytest.raises(ScraperError, match="non-JSON success response"):
            await scraper._request(
                FakeClient(),
                "https://example.com/wday/cxs/2020companies/site/jobs",
                asyncio.Semaphore(1),
                applied_facets={},
                offset=0,
            )

    asyncio.run(run())


def test_workday_from_url_delegates_to_constructor() -> None:
    """`from_url` is the explicit full-careers-URL entry point; it must
    behave exactly like passing the URL as `company_slug`."""
    url = "https://accenture.wd103.myworkdayjobs.com/accenturecareers"
    scraper = WorkdayScraper.from_url(
        url, max_fetch_seconds=5, company_name="Accenture"
    )
    assert isinstance(scraper, WorkdayScraper)
    assert scraper.company_slug == url
    assert scraper.max_fetch_seconds == 5
    assert scraper.company_name == "Accenture"


def test_workday_catalogue_lists_each_career_site_once() -> None:
    import csv
    from pathlib import Path

    from ats_scrapers.scrapers.workday import URL_PATTERN

    with Path("ats-companies/workday.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    sites = []
    for row in rows:
        match = URL_PATTERN.match(row["url"])
        assert match is not None, row["url"]
        assert row["name"].strip(), row["url"]
        sites.append(
            (
                match["company"].casefold(),
                match["instance"],
                match["site"].casefold(),
            )
        )
    assert len(set(sites)) == len(sites)


def test_workday_tenant_missing_from_its_host_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=API,
        status_code=422,
        json={"errorCode": "HTTP_422", "httpStatus": 422, "message": ""},
    )
    with pytest.raises(CompanyNotFoundError, match="moved or left Workday"):
        WorkdayScraper(URL, include_descriptions=False).fetch()


def test_workday_migrated_tenant_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=API,
        status_code=410,
        json={"errorCode": "ERR_TENANT_MIGRATED", "httpStatus": 410},
    )
    with pytest.raises(CompanyNotFoundError, match="another Workday data center"):
        WorkdayScraper(URL, include_descriptions=False).fetch()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (422, {"errorCode": "S22", "message": "invalid facet"}),
        (410, {"errorCode": "HTTP_410"}),
        (422, None),
    ],
)
def test_workday_other_410_and_422_stay_errors(httpx_mock, status, body) -> None:
    if body is None:
        httpx_mock.add_response(method="POST", url=API, status_code=status, text="<html>")
    else:
        httpx_mock.add_response(method="POST", url=API, status_code=status, json=body)
    with pytest.raises(ScraperError, match=f"returned {status}") as exc_info:
        WorkdayScraper(URL, include_descriptions=False).fetch()
    assert not isinstance(exc_info.value, CompanyNotFoundError)


def test_workday_422_on_a_filtered_search_stays_an_error() -> None:
    class FakeClient:
        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(
                status_code=422,
                headers={},
                json=lambda: {"errorCode": "HTTP_422"},
            )

    scraper = WorkdayScraper(URL)

    async def run() -> None:
        with pytest.raises(ScraperError, match="returned 422") as exc_info:
            await scraper._request(
                FakeClient(),
                API,
                asyncio.Semaphore(1),
                applied_facets={"timeType": ["ft"]},
                offset=0,
            )
        assert not isinstance(exc_info.value, CompanyNotFoundError)

    asyncio.run(run())


def test_workday_network_failure_names_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    class FakeClient:
        async def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("")

    monkeypatch.setattr("ats_scrapers.scrapers.workday.asyncio.sleep", fake_sleep)

    async def run() -> None:
        with pytest.raises(ScraperError, match=r"offset=20: ReadTimeout$"):
            await WorkdayScraper(URL)._request(
                FakeClient(),
                API,
                asyncio.Semaphore(1),
                applied_facets={},
                offset=20,
            )

    asyncio.run(run())
