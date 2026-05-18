"""Guardrails for pathological Workday tenants."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import scripts.run_pipeline as runner
from jobhive.exceptions import ScraperError
from jobhive.scrapers.workday import WorkdayScraper


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

    monkeypatch.setattr("jobhive.scrapers.workday.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("jobhive.scrapers.workday.MAX_RETRY_DELAY", 7.0)

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


def test_workday_runner_sets_tenant_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBHIVE_WORKDAY_TENANT_TIMEOUT", "123")
    kwargs = runner.CONFIGS["workday"]["kwargs"]({})

    assert kwargs == {"max_fetch_seconds": 123.0}
