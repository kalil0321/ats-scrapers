"""Guardrails for pathological Workday tenants."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import scripts.run_pipeline as runner
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.scrapers.workday import WorkdayScraper


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
        with pytest.raises(ScraperError, match="gave up"):
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


def test_workday_runner_sets_tenant_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATS_SCRAPERS_WORKDAY_TENANT_TIMEOUT", "123")
    kwargs = runner.CONFIGS["workday"]["kwargs"]({"name": "Advocate Health"})

    assert kwargs == {
        "max_fetch_seconds": 123.0,
        "company_name": "Advocate Health",
    }
