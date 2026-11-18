"""Workday sends its requests through httpcloak when it is installed."""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers import workday
from ats_scrapers.scrapers.workday import WorkdayScraper

URL = "https://acme.wd5.myworkdayjobs.com/External"
API = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
DETAIL = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"


def _answer(status: int, payload: Any = None, headers: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        headers=headers if headers is not None else {"content-type": ["application/json"]},
        text=json.dumps(payload) if payload is not None else "",
    )


class _Httpcloak:
    """Stands in for the httpcloak module and records its sessions."""

    def __init__(self, handler: Any) -> None:
        self.sessions: list[Any] = []
        self.calls: list[tuple[str, str, Any, dict]] = []
        outer = self

        class Session:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                self.closed = False
                outer.sessions.append(self)

            def post(self, url: str, json: Any = None, headers: dict | None = None) -> Any:
                outer.calls.append(("POST", url, json, headers or {}))
                return handler("POST", url, json)

            def get(self, url: str, headers: dict | None = None) -> Any:
                outer.calls.append(("GET", url, None, headers or {}))
                return handler("GET", url, None)

            def close(self) -> None:
                self.closed = True

        self.Session = Session


@pytest.fixture
def cloak(monkeypatch: pytest.MonkeyPatch):
    def install(handler: Any) -> _Httpcloak:
        fake = _Httpcloak(handler)
        monkeypatch.setitem(sys.modules, "httpcloak", fake)
        monkeypatch.setattr(WorkdayScraper, "fetch_engine", "cloak")
        return fake

    return install


def _board(method: str, url: str, body: Any) -> Any:
    if method == "GET":
        return _answer(200, {"jobPostingInfo": {"jobDescription": f"<p>About {url[-3:]}</p>"}})
    offset = body["offset"]
    keys = [f"R{i:02d}" for i in range(25)][offset:offset + 20]
    return _answer(200, {
        "total": 25,
        "jobPostings": [{"title": k, "externalPath": f"/job/X/Job_{k}"} for k in keys],
    })


def test_listing_and_descriptions_go_through_httpcloak(
    cloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_httpx(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("httpx must not be used")

    monkeypatch.setattr(workday.httpx, "AsyncClient", no_httpx)
    fake = cloak(_board)

    jobs = WorkdayScraper(URL, proxy="http://user:pass@proxy.test:8080").fetch()

    assert sorted(job.description for job in jobs) == [f"About R{i:02d}" for i in range(25)]
    (session,) = fake.sessions
    assert session.kwargs == {"timeout": 30, "proxy": "http://user:pass@proxy.test:8080"}
    assert session.closed
    posts = [call for call in fake.calls if call[0] == "POST"]
    assert [call[2]["offset"] for call in posts] == [0, 20]
    assert all(call[1] == API for call in posts)
    assert posts[0][3]["Content-Type"] == "application/json"
    gets = [call for call in fake.calls if call[0] == "GET"]
    assert len(gets) == 25
    assert all("User-Agent" not in call[3] for call in gets)


def test_descriptions_of_a_site_share_one_httpcloak_session(cloak) -> None:
    fake = cloak(_board)
    scraper = WorkdayScraper(URL)
    job = Job(
        url=f"{URL}/job/X/Job_R01", title="Job", company="Acme",
        ats_type=ATSType.WORKDAY, ats_id="R01",
    )

    assert scraper.get_description(job) == "About R01"
    assert scraper.get_description(job) == "About R01"
    assert [call[1] for call in fake.calls] == [f"{DETAIL}/job/X/Job_R01"] * 2
    (session,) = fake.sessions

    scraper.close()

    assert session.closed


def test_an_httpcloak_failure_is_retried_and_named(
    cloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    def refuse(method: str, url: str, body: Any) -> Any:
        raise RuntimeError("dial tcp: i/o timeout")

    monkeypatch.setattr(workday.asyncio, "sleep", no_sleep)
    fake = cloak(refuse)

    with pytest.raises(ScraperError, match=r"offset=0: TransportError: dial tcp: i/o timeout$"):
        WorkdayScraper(URL, include_descriptions=False).fetch()
    assert len(fake.calls) == workday.MAX_RETRIES


def test_response_headers_given_as_lists_are_read() -> None:
    response = workday._CloakResponse(
        _answer(429, None, headers={"Retry-After": ["5"], "cf-mitigated": "challenge"})
    )

    assert response.headers.get("retry-after") == "5"
    assert response.headers.get("CF-Mitigated") == "challenge"


def test_an_unusable_httpcloak_session_is_a_scraper_error(cloak) -> None:
    fake = cloak(_board)

    def broken(**_kwargs: Any) -> None:
        raise OSError("library not loaded")

    fake.Session = broken

    with pytest.raises(ScraperError, match="httpcloak could not start"):
        WorkdayScraper(URL).fetch()


def test_the_session_is_freed_after_its_last_request(cloak) -> None:
    started, release = threading.Event(), threading.Event()

    def slow(method: str, url: str, body: Any) -> Any:
        started.set()
        release.wait(5)
        return _answer(200, {})

    fake = cloak(slow)
    client = workday._CloakClient(timeout=30, proxy=None)
    worker = threading.Thread(target=client.get, args=(f"{DETAIL}/job/X/Job_R01",))
    worker.start()
    started.wait(5)

    client.close()
    assert not fake.sessions[0].closed
    release.set()
    worker.join(5)

    assert fake.sessions[0].closed
    with pytest.raises(RuntimeError, match="closed"):
        client.get(f"{DETAIL}/job/X/Job_R02")


def test_without_httpcloak_requests_go_through_httpx(
    monkeypatch: pytest.MonkeyPatch, httpx_mock, caplog
) -> None:
    monkeypatch.setitem(sys.modules, "httpcloak", None)
    monkeypatch.setattr(WorkdayScraper, "fetch_engine", "cloak")
    monkeypatch.setattr(workday, "_warned_without_cloak", False)
    httpx_mock.add_response(
        method="POST", url=API,
        json={"total": 1, "jobPostings": [{"title": "Job", "externalPath": "/job/X/Job_R1"}]},
        is_reusable=True,
    )

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.workday"):
        jobs = WorkdayScraper(URL, include_descriptions=False).fetch()
        WorkdayScraper(URL, include_descriptions=False).fetch()

    assert len(jobs) == 1
    assert caplog.text.count("httpcloak is not installed") == 1


def test_the_environment_proxy_is_used_by_httpcloak(
    cloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.test:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    fake = cloak(lambda method, url, body: _answer(200, {"total": 0, "jobPostings": []}))

    WorkdayScraper(URL, include_descriptions=False).fetch()

    assert fake.sessions[0].kwargs["proxy"] == "http://env-proxy.test:3128"


def test_a_scraper_set_to_httpx_does_not_use_httpcloak(
    monkeypatch: pytest.MonkeyPatch, httpx_mock
) -> None:
    monkeypatch.setitem(sys.modules, "httpcloak", SimpleNamespace(Session=None))
    monkeypatch.setattr(WorkdayScraper, "fetch_engine", "cloak")
    httpx_mock.add_response(method="POST", url=API, json={"total": 0, "jobPostings": []})
    scraper = WorkdayScraper(URL, include_descriptions=False)
    scraper.fetch_engine = "httpx"

    assert scraper.fetch() == []
    assert len(httpx_mock.get_requests()) == 1


def _board_over_httpx(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content) if request.method == "POST" else None
    answer = _board(request.method, str(request.url), body)
    return httpx.Response(answer.status_code, text=answer.text)


class _Kept(httpx.MockTransport):
    """A caller's transport, which records whether anything closed it."""

    closed: list[str]

    def close(self) -> None:
        self.closed.append("close")

    async def aclose(self) -> None:
        self.closed.append("aclose")


def test_a_transport_carries_every_request_instead_of_httpcloak(cloak) -> None:
    fake = cloak(_board)
    transport = _Kept(_board_over_httpx)
    transport.closed = []
    scraper = WorkdayScraper(URL, proxy="http://proxy.test:8080", transport=transport)

    jobs = scraper.fetch()
    bare = jobs[0].model_copy(update={"description": None})
    description = scraper.get_description(bare)
    scraper.close()

    assert sorted(job.description for job in jobs) == [f"About R{i:02d}" for i in range(25)]
    assert description == jobs[0].description
    assert fake.sessions == []
    assert transport.closed == []


def test_get_description_needs_a_transport_that_sends_synchronously() -> None:
    class AsyncOnly(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

    job = Job(
        url=f"{URL}/job/X/Job_R01", title="Job", company="Acme",
        ats_type=ATSType.WORKDAY, ats_id="R01",
    )

    with pytest.raises(TypeError, match="BaseTransport"):
        WorkdayScraper(URL, transport=AsyncOnly()).get_description(job)


def test_a_pacer_is_asked_before_every_request() -> None:
    class Pacer:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def wait(self, url: str) -> None:
            self.urls.append(url)

        def wait_sync(self, url: str) -> None:
            self.urls.append(url)

    pacer = Pacer()
    scraper = WorkdayScraper(URL, transport=httpx.MockTransport(_board_over_httpx), pacer=pacer)

    jobs = scraper.fetch()
    scraper.get_description(jobs[0].model_copy(update={"description": None}))

    assert pacer.urls.count(API) == 2
    assert len([url for url in pacer.urls if url.startswith(f"{DETAIL}/job/")]) == 26
