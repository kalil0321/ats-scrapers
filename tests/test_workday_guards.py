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


class _Answers:
    """Stands in for the listing client: the same answer to every search."""

    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(status_code=self.status, headers=self.headers, json=_no_json)


def _no_json():
    raise ValueError("no JSON body")


def _search(scraper: WorkdayScraper, client: object) -> None:
    async def run() -> None:
        await scraper._request(
            client, API, asyncio.Semaphore(1), applied_facets={}, offset=0,
        )

    asyncio.run(run())


@pytest.fixture
def delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waited: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waited.append(delay)

    monkeypatch.setattr("ats_scrapers.scrapers.workday.asyncio.sleep", fake_sleep)
    return waited


def test_workday_retry_after_is_waited(delays: list[float]) -> None:
    client = _Answers(429, {"Retry-After": "2"})

    with pytest.raises(ScraperError, match=r"gave up after 3 retries at offset=0: HTTP 429$"):
        _search(WorkdayScraper(URL), client)

    assert client.calls == 3
    assert delays == [2.0, 2.0]


def test_workday_retry_after_above_the_cap_ends_the_board(
    monkeypatch: pytest.MonkeyPatch, delays: list[float]
) -> None:
    monkeypatch.setattr("ats_scrapers.scrapers.workday.MAX_RETRY_DELAY", 7.0)
    client = _Answers(429, {"Retry-After": "3600"})

    with pytest.raises(ScraperError, match=r"offset=0: HTTP 429, Retry-After 3600 s"):
        _search(WorkdayScraper(URL), client)

    assert client.calls == 1
    assert delays == []


def test_workday_rate_limit_without_retry_after_backs_off_with_jitter(
    delays: list[float],
) -> None:
    with pytest.raises(ScraperError, match=r"HTTP 403$"):
        _search(WorkdayScraper(URL), _Answers(403, {}))

    first, second = delays
    assert 2.5 <= first <= 5.0
    assert 5.0 <= second <= 10.0


def test_workday_bot_challenge_is_not_retried(delays: list[float]) -> None:
    client = _Answers(429, {"cf-mitigated": "challenge", "Content-Type": "text/html"})

    with pytest.raises(ScraperError, match=r"offset=0: HTTP 429, a bot challenge$"):
        _search(WorkdayScraper(URL), client)

    assert client.calls == 1
    assert delays == []


def test_workday_does_not_wait_past_its_deadline(delays: list[float]) -> None:
    scraper = WorkdayScraper(URL, max_fetch_seconds=60)
    scraper._deadline = time.monotonic() + 5

    with pytest.raises(ScraperError, match=r"max_fetch_seconds=60: .*\(waiting out HTTP 429\)"):
        _search(scraper, _Answers(429, {"Retry-After": "10"}))

    assert delays == []


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("12", 12.0), ("0.5", 0.5), ("-3", 0.0), ("soon", None), ("", None), (None, None)],
)
def test_workday_retry_after_values(value, seconds) -> None:
    from ats_scrapers.scrapers.workday import _retry_after

    assert _retry_after(value) == seconds


def test_workday_retry_after_as_a_date() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from ats_scrapers.scrapers.workday import _retry_after

    when = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)

    assert 15 <= (_retry_after(when) or 0) <= 20


def test_workday_descriptions_stop_at_a_rate_limit(caplog) -> None:
    from ats_scrapers.models import ATSType, Job

    class Details:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(status_code=429, headers={"cf-mitigated": "challenge"})

    jobs = [
        Job(url=f"{URL}/job/X/Job_R{n}", title="Job", company="Acme",
            ats_type=ATSType.WORKDAY, ats_id=f"R{n}")
        for n in range(4)
    ]
    client = Details()

    async def run() -> None:
        await WorkdayScraper(URL)._enrich_details(
            client, asyncio.Semaphore(1), "https://acme.wd5.myworkdayjobs.com/x", jobs,
        )

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.workday"):
        asyncio.run(run())

    assert client.calls == 1
    assert all(job.description is None for job in jobs)
    assert "got HTTP 429" in caplog.text


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
    assert len(delays) == 1
    assert 0.5 <= delays[0] <= 1.0


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


def test_workday_closed_site_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=API,
        status_code=403,
        json={"errorCode": "S22", "httpStatus": 403, "message": "permission denied", "messageParams": {}},
    )
    with pytest.raises(CompanyNotFoundError, match="closed this site"):
        WorkdayScraper(URL, include_descriptions=False).fetch()
    # Not found at once: no retries or slower routes for a closed site.
    assert len(httpx_mock.get_requests()) == 1


def test_workday_closed_tenant_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=API,
        status_code=401,
        json={
            "errorCode": "HTTP_401",
            "errorCaseId": "5322FFMWVW0XH1",
            "httpStatus": 401,
            "message": "Unable to verify credentials for system account 39$4009",
            "messageParams": {},
        },
    )
    with pytest.raises(CompanyNotFoundError, match="cannot verify its public account"):
        WorkdayScraper(URL, include_descriptions=False).fetch()
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"errorCode": "HTTP_401", "httpStatus": 401, "message": ""},
        {"errorCode": "HTTP_401", "httpStatus": 401, "message": "Invalid CSRF token"},
        {"errorCode": "S22", "message": "Unable to verify credentials for system account 39$1"},
    ],
)
def test_workday_other_401_stays_an_error(httpx_mock, body) -> None:
    if body is None:
        httpx_mock.add_response(method="POST", url=API, status_code=401, text="<html>")
    else:
        httpx_mock.add_response(method="POST", url=API, status_code=401, json=body)
    with pytest.raises(ScraperError, match="returned 401") as exc_info:
        WorkdayScraper(URL, include_descriptions=False).fetch()
    assert not isinstance(exc_info.value, CompanyNotFoundError)


def test_workday_401_on_a_filtered_search_stays_an_error() -> None:
    class FakeClient:
        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(
                status_code=401,
                headers={},
                json=lambda: {
                    "errorCode": "HTTP_401",
                    "message": "Unable to verify credentials for system account 39$1",
                },
            )

    async def run() -> None:
        with pytest.raises(ScraperError, match="returned 401") as exc_info:
            await WorkdayScraper(URL)._request(
                FakeClient(),
                API,
                asyncio.Semaphore(1),
                applied_facets={},
                offset=20,
            )
        assert not isinstance(exc_info.value, CompanyNotFoundError)

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"errorCode": "S21", "httpStatus": 403, "message": "permission denied"},
        {"errorCode": "S22", "httpStatus": 403, "message": "request blocked"},
        {"errorCode": "S22", "httpStatus": 403},
        {"errorCode": "S22", "httpStatus": 403, "message": None},
        ["S22", "permission denied"],
    ],
)
def test_workday_other_403_is_retried_as_a_refusal(
    httpx_mock, monkeypatch: pytest.MonkeyPatch, body
) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    for _ in range(3):
        if body is None:
            httpx_mock.add_response(method="POST", url=API, status_code=403, text="<html>Access denied</html>")
        else:
            httpx_mock.add_response(method="POST", url=API, status_code=403, json=body)
    with pytest.raises(ScraperError, match="HTTP 403") as exc_info:
        WorkdayScraper(URL, include_descriptions=False).fetch()
    assert not isinstance(exc_info.value, CompanyNotFoundError)
    assert len(httpx_mock.get_requests()) == 3


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


async def test_a_failed_search_stops_the_other_searches_first(httpx_mock) -> None:
    """The other searches of the board end before the client closes."""
    import json

    other_started = asyncio.Event()
    other_ended: list[str] = []

    async def reply(request: httpx.Request) -> httpx.Response:
        applied = json.loads(request.content)["appliedFacets"]
        if not applied:
            return httpx.Response(200, json={
                "total": 2000,
                "jobPostings": [],
                "facets": [{"facetParameter": "jobFamilyGroup", "values": [
                    {"id": "failing", "count": 1500}, {"id": "slow", "count": 1500},
                ]}],
            })
        if applied["jobFamilyGroup"] == ["failing"]:
            await other_started.wait()
            return httpx.Response(400)
        other_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            other_ended.append("cancelled")
            raise
        return httpx.Response(200, json={"total": 45, "jobPostings": []})

    httpx_mock.add_callback(reply, url=API, is_reusable=True)

    with pytest.raises(ScraperError, match="returned 400"):
        await asyncio.wait_for(WorkdayScraper(URL, include_descriptions=False).afetch(), 5)

    assert other_ended == ["cancelled"]
    assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []


async def test_run_all_raises_the_first_error_after_the_others_end() -> None:
    from ats_scrapers.scrapers.workday import _run_all

    ended: list[str] = []

    async def fail() -> None:
        await asyncio.sleep(0)
        raise ScraperError("first")

    async def wait() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            ended.append("other")

    with pytest.raises(ScraperError, match="first"):
        await _run_all([fail(), wait(), wait()])

    assert ended == ["other", "other"]
    assert await _run_all([asyncio.sleep(0, result=n) for n in (1, 2)]) == [1, 2]


class _OnePage:
    """A listing that announces ``total`` jobs on a single short page."""

    def __init__(self, total: int, facets: list | None = None) -> None:
        self.total = total
        self.facets = facets or []

    async def post(self, *_args, **_kwargs):
        return SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {"total": self.total, "jobPostings": [], "facets": self.facets},
        )


@pytest.mark.parametrize(
    ("seconds_per_job", "depth", "extra"),
    [(0.5, 0, 5.0), (0.0, 0, 0.0), (0.5, 1, 0.0)],
)
def test_workday_board_gets_time_for_each_job_it_announces(seconds_per_job, depth, extra) -> None:
    scraper = WorkdayScraper(URL, max_fetch_seconds=60, seconds_per_job=seconds_per_job)
    start = time.monotonic() + 100
    scraper._deadline = start

    async def run() -> None:
        await scraper._exhaust_query(
            _OnePage(10), API, asyncio.Semaphore(1),
            applied_facets={}, absorb=lambda _postings: None, depth=depth,
        )

    asyncio.run(run())
    # Only the board's own first search sizes the budget, not a filtered one.
    assert scraper._deadline == pytest.approx(start + extra)


def test_workday_deadline_error_names_the_time_per_job() -> None:
    scraper = WorkdayScraper(URL, max_fetch_seconds=900, seconds_per_job=0.1)
    assert "max_fetch_seconds=900 plus 0.1 s per job" in str(scraper._deadline_error())


def test_workday_capped_board_gets_time_for_the_jobs_its_facets_count() -> None:
    facets = [{
        "facetParameter": "timeType",
        "values": [{"id": "ft", "descriptor": "Full", "count": 6000}, {"id": "pt", "descriptor": "Part", "count": 4000}],
    }]

    class CappedRoot:
        async def post(self, *_args, json=None, **_kwargs):
            # The board's own search is capped at 2,000; each filtered one is small.
            filtered = bool((json or {}).get("appliedFacets"))
            return SimpleNamespace(
                status_code=200,
                headers={},
                json=lambda: {"total": 10 if filtered else 2000, "jobPostings": [], "facets": facets},
            )

    scraper = WorkdayScraper(URL, max_fetch_seconds=60, seconds_per_job=0.1)
    start = time.monotonic() + 100
    scraper._deadline = start

    async def run() -> None:
        await scraper._exhaust_query(
            CappedRoot(), API, asyncio.Semaphore(1),
            applied_facets={}, absorb=lambda _postings: None, depth=0,
        )

    asyncio.run(run())
    # Time for the 10,000 jobs the facet counts, once, not for the capped 2,000.
    assert scraper._deadline == pytest.approx(start + 1000)


def test_workday_retry_past_the_deadline_names_the_time_per_job(monkeypatch: pytest.MonkeyPatch) -> None:
    scraper = WorkdayScraper(URL, max_fetch_seconds=60, seconds_per_job=0.1)
    scraper._deadline = time.monotonic() + 5

    async def run() -> None:
        await scraper._wait_to_retry(10, "HTTP 429")

    with pytest.raises(ScraperError, match=r"max_fetch_seconds=60 plus 0.1 s per job: .*\(waiting out HTTP 429\)"):
        asyncio.run(run())

