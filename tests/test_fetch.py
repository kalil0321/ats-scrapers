"""Tests for the shared HTTP fetch layer (`ats_scrapers.fetch`)."""

from __future__ import annotations

import asyncio
import socket
import ssl
import sys
import types
from typing import Any

import httpx
import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.fetch import (
    Fetcher,
    FetchResponse,
    RetryExhaustedError,
    certificate_error,
    proxy_url_from_env,
)

URL = "https://api.example.com/jobs"


def _fetcher(**kwargs: Any) -> Fetcher:
    kwargs.setdefault("label", "Example board acme")
    return Fetcher(**kwargs)


# --- status mapping ---------------------------------------------------------


async def test_get_json_returns_payload(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"jobs": [1, 2]})
    async with _fetcher() as fetch:
        assert await fetch.get_json(URL) == {"jobs": [1, 2]}


async def test_get_text_returns_body(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text="<html>hi</html>")
    async with _fetcher() as fetch:
        assert await fetch.get_text(URL) == "<html>hi</html>"


async def test_post_json_sends_body(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"ok": True}, method="POST")
    async with _fetcher() as fetch:
        assert await fetch.post_json(URL, json={"limit": 20}) == {"ok": True}
    request = httpx_mock.get_requests()[0]
    assert b'"limit": 20' in request.read() or b'"limit":20' in request.read()


async def test_404_maps_to_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=404)
    async with _fetcher() as fetch:
        with pytest.raises(CompanyNotFoundError, match="Example board acme"):
            await fetch.get_json(URL)


async def test_unexpected_4xx_raises_scraper_error(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=418)
    async with _fetcher() as fetch:
        with pytest.raises(ScraperError, match="418"):
            await fetch.get_json(URL)


async def test_handled_statuses_are_returned_unmapped(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=410, text="gone")
    async with _fetcher() as fetch:
        response = await fetch.request("GET", URL, handled={410})
        assert response.status_code == 410
        assert response.text == "gone"


# --- retries ----------------------------------------------------------------


async def test_5xx_retries_then_succeeds(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=502)
    httpx_mock.add_response(url=URL, json={"ok": True})
    async with _fetcher() as fetch:
        assert await fetch.get_json(URL) == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2


async def test_5xx_exhausts_retries(httpx_mock) -> None:
    for _ in range(3):
        httpx_mock.add_response(url=URL, status_code=503)
    async with _fetcher() as fetch:
        with pytest.raises(ScraperError, match="503"):
            await fetch.get_json(URL)
    assert len(httpx_mock.get_requests()) == 3


async def test_429_respects_numeric_retry_after(httpx_mock, monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "7"})
    httpx_mock.add_response(url=URL, json={})
    async with _fetcher() as fetch:
        await fetch.get_json(URL)
    assert sleeps == [7.0]


def _closed(response: FetchResponse) -> str | None:
    return "is closed" if "closed" in response.text else None


async def test_gone_response_is_not_found_without_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=503, text="board closed")
    async with _fetcher() as fetch:
        with pytest.raises(CompanyNotFoundError, match="Example board acme: is closed"):
            await fetch.get_json(URL, gone=_closed)
    assert len(httpx_mock.get_requests()) == 1


async def test_other_response_keeps_retry_after_and_attempts_with_gone(
    httpx_mock, monkeypatch
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    httpx_mock.add_response(url=URL, status_code=503, headers={"Retry-After": "7"})
    httpx_mock.add_response(url=URL, status_code=503, headers={"Retry-After": "9"})
    httpx_mock.add_response(url=URL, status_code=503)
    async with _fetcher() as fetch:
        with pytest.raises(RetryExhaustedError, match="503 after 3 attempts"):
            await fetch.get_json(URL, gone=_closed)
    assert sleeps == [7.0, 9.0]
    assert len(httpx_mock.get_requests()) == 3


async def test_gone_is_not_asked_about_a_success(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"text": "closed"})
    async with _fetcher() as fetch:
        assert await fetch.get_json(URL, gone=_closed) == {"text": "closed"}


async def test_retry_after_non_string_falls_back_without_crashing(monkeypatch) -> None:
    responses = iter(
        [
            FetchResponse(429, "", {"Retry-After": ["1"]}),  # type: ignore[dict-item]
            FetchResponse(200, "{}", {}),
        ]
    )
    sleeps: list[float] = []

    async def fake_perform(*args: Any, **kwargs: Any) -> FetchResponse:
        return next(responses)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with _fetcher(retry_base_delay=1.5, max_retry_delay=30.0) as fetch:
        monkeypatch.setattr(fetch, "_perform", fake_perform)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        assert await fetch.get_json(URL) == {}
    assert sleeps == [1.5]


async def test_network_errors_retry(httpx_mock) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("boom"))
    httpx_mock.add_response(url=URL, json={"ok": 1})
    async with _fetcher() as fetch:
        assert await fetch.get_json(URL) == {"ok": 1}


def _connect_error(cause: BaseException) -> Exception:
    import httpx

    try:
        raise httpx.ConnectError("connect failed") from cause
    except httpx.ConnectError as exc:
        return exc


def _certificate_error(verify_code: int) -> ssl.SSLCertVerificationError:
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_code = verify_code
    return error


@pytest.mark.parametrize(
    ("cause", "reason", "attempts"),
    [
        (
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            "does not resolve",
            1,
        ),
        (
            socket.gaierror(socket.EAI_NODATA, "No address associated with hostname"),
            "does not resolve",
            1,
        ),
        (_certificate_error(62), "certificate for another name", 3),
    ],
)
async def test_missing_host_is_not_found_when_enabled(
    httpx_mock, monkeypatch, cause: BaseException, reason: str, attempts: int
) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    httpx_mock.add_exception(_connect_error(cause), is_reusable=True)
    async with _fetcher(missing_host_not_found=True) as fetch:
        with pytest.raises(CompanyNotFoundError, match=reason):
            await fetch.get_text(URL)
    assert len(httpx_mock.get_requests()) == attempts


async def test_lookup_that_fails_for_a_moment_is_retried(httpx_mock, monkeypatch) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    httpx_mock.add_exception(
        _connect_error(socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"))
    )
    httpx_mock.add_response(url=URL, text="ok")
    async with _fetcher(missing_host_not_found=True) as fetch:
        assert await fetch.get_text(URL) == "ok"


async def test_missing_host_is_retried_by_default(httpx_mock) -> None:
    error = _connect_error(socket.gaierror(socket.EAI_NONAME, "unknown"))
    httpx_mock.add_exception(error, is_reusable=True)
    async with _fetcher() as fetch:
        with pytest.raises(RetryExhaustedError):
            await fetch.get_text(URL)


async def test_failed_lookup_behind_a_proxy_is_not_a_missing_host(httpx_mock) -> None:
    error = _connect_error(socket.gaierror(socket.EAI_NONAME, "unknown"))
    httpx_mock.add_exception(error, is_reusable=True)
    async with _fetcher(missing_host_not_found=True, proxy="http://proxy.invalid:8080") as fetch:
        with pytest.raises(RetryExhaustedError):
            await fetch.get_text(URL)


async def test_failed_lookup_behind_an_environment_proxy_is_not_a_missing_host(
    httpx_mock, monkeypatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    error = _connect_error(socket.gaierror(socket.EAI_NONAME, "unknown"))
    httpx_mock.add_exception(error, is_reusable=True)
    async with _fetcher(missing_host_not_found=True) as fetch:
        with pytest.raises(RetryExhaustedError):
            await fetch.get_text(URL)


async def test_expired_certificate_is_not_a_missing_host(httpx_mock) -> None:
    error = _connect_error(_certificate_error(10))
    for _ in range(3):
        httpx_mock.add_exception(error)
    async with _fetcher(missing_host_not_found=True) as fetch:
        with pytest.raises(RetryExhaustedError) as excinfo:
            await fetch.get_text(URL)
    assert certificate_error(excinfo.value).verify_code == 10


def test_certificate_error_is_none_for_other_failures() -> None:
    error = _connect_error(socket.gaierror(socket.EAI_NONAME, "unknown"))
    assert certificate_error(error) is None


_AKAMAI_DNS_FAILURE = (
    "<HTML><HEAD><TITLE>Service Unavailable</TITLE></HEAD><BODY>"
    "<H1>Service Unavailable - DNS failure</H1>"
    "The server is temporarily unable to service your request.</BODY></HTML>"
)


async def test_cdn_without_origin_is_not_found_when_enabled(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=503, text=_AKAMAI_DNS_FAILURE, is_reusable=True)
    async with _fetcher(missing_host_not_found=True) as fetch:
        with pytest.raises(CompanyNotFoundError, match="no origin left behind its CDN"):
            await fetch.get_text(URL)
    assert len(httpx_mock.get_requests()) == 3


async def test_cdn_without_origin_is_retried_by_default(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=503, text=_AKAMAI_DNS_FAILURE, is_reusable=True)
    async with _fetcher() as fetch:
        with pytest.raises(RetryExhaustedError):
            await fetch.get_text(URL)


async def test_cdn_origin_that_comes_back_is_fetched(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=503, text=_AKAMAI_DNS_FAILURE)
    httpx_mock.add_response(url=URL, text="ok")
    async with _fetcher(missing_host_not_found=True) as fetch:
        assert await fetch.get_text(URL) == "ok"


async def test_other_503_is_not_a_missing_host(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=503, text="Service Unavailable", is_reusable=True)
    async with _fetcher(missing_host_not_found=True) as fetch:
        with pytest.raises(RetryExhaustedError):
            await fetch.get_text(URL)


async def test_retries_config_is_respected(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=500)
    async with _fetcher(retries=1) as fetch:
        with pytest.raises(ScraperError):
            await fetch.get_json(URL)
    assert len(httpx_mock.get_requests()) == 1


async def test_408_is_retried(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=408)
    httpx_mock.add_response(url=URL, json={"ok": True})
    async with _fetcher() as fetch:
        assert await fetch.get_json(URL) == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2


async def test_malformed_json_raises_scraper_error(httpx_mock) -> None:
    from ats_scrapers.fetch import MalformedJSONError

    httpx_mock.add_response(url=URL, text="<html>WAF page</html>")
    async with _fetcher() as fetch:
        with pytest.raises(ScraperError, match="not valid JSON") as excinfo:
            await fetch.get_json(URL)
    # Subclasses ValueError too, so pre-existing best-effort wrappers
    # (`except ValueError`) keep catching it.
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, MalformedJSONError)
    assert URL in str(excinfo.value)


# --- escalation -------------------------------------------------------------


class _FakeCloakResponse:
    def __init__(
        self,
        status_code: int = 200,
        text: str = "{}",
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _install_fake_httpcloak(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]],
    response: _FakeCloakResponse | None = None,
) -> None:
    module = types.ModuleType("httpcloak")

    def get(url: str, **kwargs: Any) -> _FakeCloakResponse:
        calls.append({"url": url, **kwargs})
        return response or _FakeCloakResponse()

    module.get = get  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpcloak", module)


async def test_403_escalates_to_cloak_when_enabled(httpx_mock, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_httpcloak(
        monkeypatch, calls, _FakeCloakResponse(text='{"jobs": []}')
    )
    httpx_mock.add_response(url=URL, status_code=406)
    async with _fetcher(escalate=True, timeout=2.5) as fetch:
        payload = await fetch.get_json(URL)
    assert payload == {"jobs": []}
    assert calls and calls[0]["url"] == URL
    # The fetcher stays escalated: the next request goes straight to cloak.
    async with asyncio.timeout(5):
        await fetch.get_json(URL)
    assert [call["timeout"] for call in calls] == [2.5, 2.5]


async def test_escalation_gets_fresh_attempt_budget(httpx_mock, monkeypatch) -> None:
    """A 403 on the FINAL httpx attempt (retries=1) must still escalate."""
    calls: list[dict[str, Any]] = []
    _install_fake_httpcloak(monkeypatch, calls, _FakeCloakResponse(text='{"ok": 1}'))
    httpx_mock.add_response(url=URL, status_code=403)
    async with _fetcher(escalate=True, retries=1) as fetch:
        assert await fetch.get_json(URL) == {"ok": 1}
    assert len(calls) == 1


async def test_escalation_leaves_the_user_agent_to_httpcloak(httpx_mock, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_httpcloak(monkeypatch, calls, _FakeCloakResponse(text="{}"))
    httpx_mock.add_response(url=URL, status_code=403)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    async with _fetcher(escalate=True, headers=headers) as fetch:
        await fetch.get_json(URL)
    assert calls[0]["headers"] == {"Accept": "application/json"}
    assert headers["User-Agent"] == "Mozilla/5.0"


async def test_cloak_engine_keeps_redirects_when_asked(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_httpcloak(
        monkeypatch,
        calls,
        _FakeCloakResponse(status_code=302, text="", headers={"Location": ["/moved"]}),
    )
    async with _fetcher(engine="cloak", follow_redirects=False) as fetch:
        response = await fetch.request("GET", URL, handled={302})
    assert calls[0]["allow_redirects"] is False
    assert response.headers == {"Location": "/moved"}


async def test_403_without_escalate_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=403)
    async with _fetcher() as fetch:
        with pytest.raises(ScraperError, match="403"):
            await fetch.get_json(URL)


async def test_pinned_cloak_engine_never_touches_httpx(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_httpcloak(monkeypatch, calls, _FakeCloakResponse(text="[1]"))
    async with _fetcher(engine="cloak") as fetch:
        assert await fetch.get_json(URL) == [1]
    assert len(calls) == 1
    assert calls[0]["timeout"] == 30


# --- proxy config -----------------------------------------------------------


def test_proxy_env_standard_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATS_SCRAPERS_PROXY", "http://user:pass@proxy.example:8080")
    assert proxy_url_from_env() == "http://user:pass@proxy.example:8080"


def test_proxy_env_legacy_host_port_user_pass_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATS_SCRAPERS_PROXY", raising=False)
    monkeypatch.setenv("PROXY", "http://proxy.example:8080:user:pass")
    assert proxy_url_from_env() == "http://user:pass@proxy.example:8080"


def test_proxy_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATS_SCRAPERS_PROXY", raising=False)
    monkeypatch.delenv("PROXY", raising=False)
    assert proxy_url_from_env() is None


def test_malformed_legacy_proxy_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATS_SCRAPERS_PROXY", raising=False)
    monkeypatch.setenv("PROXY", "not-a-proxy")
    assert proxy_url_from_env() is None


# --- FetchResponse ----------------------------------------------------------


def test_fetch_response_json_parses_text() -> None:
    response = FetchResponse(200, '{"a": 1}', {})
    assert response.json() == {"a": 1}


async def test_response_reports_the_url_that_answered(httpx_mock) -> None:
    moved = "https://www.example.com/careers"
    httpx_mock.add_response(url=URL, status_code=301, headers={"Location": moved})
    httpx_mock.add_response(url=moved, text="ok")
    async with _fetcher() as fetch:
        response = await fetch.request("GET", URL)
    assert response.url == moved


# --- sync wrapper on BaseScraper -------------------------------------------


def test_run_sync_outside_loop() -> None:
    from ats_scrapers.scrapers.base import BaseScraper

    async def coro() -> int:
        return 42

    assert BaseScraper._run_sync(coro()) == 42


async def test_run_sync_inside_running_loop() -> None:
    """fetch() from a Jupyter-style running loop must not crash."""
    from ats_scrapers.scrapers.base import BaseScraper

    async def coro() -> int:
        await asyncio.sleep(0)
        return 7

    assert BaseScraper._run_sync(coro()) == 7


# --- transport --------------------------------------------------------------


class _Recorder(httpx.AsyncBaseTransport):
    def __init__(self, status: int = 200, payload: Any = None) -> None:
        self.status = status
        self.payload = payload
        self.urls: list[str] = []
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(self.status, json=self.payload)

    async def aclose(self) -> None:
        self.closed = True


async def test_a_transport_sends_every_request_and_stays_open() -> None:
    transport = _Recorder(payload={"ok": True})

    async with _fetcher(transport=transport, proxy="http://proxy.test:8080") as fetch:
        assert await fetch.get_json(URL) == {"ok": True}

    assert transport.urls == [URL]
    assert not transport.closed


async def test_a_transport_turns_escalation_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from ats_scrapers import fetch as fetch_module

    monkeypatch.setattr(fetch_module, "_cloak_available", lambda: True)

    async with _fetcher(transport=_Recorder(status=403), escalate=True) as fetch:
        with pytest.raises(ScraperError, match="returned 403"):
            await fetch.get_json(URL)
        assert fetch.engine == "httpx"


def test_the_cloak_engine_cannot_use_a_transport() -> None:
    with pytest.raises(ValueError, match="cloak engine"):
        _fetcher(engine="cloak", transport=_Recorder())


def test_scrapers_give_their_transport_to_their_fetchers() -> None:
    from ats_scrapers.scrapers import AshbyScraper

    transport = _Recorder(payload={"jobs": []})

    assert AshbyScraper("acme", transport=transport).fetch() == []
    assert transport.urls[0].startswith("https://api.ashbyhq.com/posting-api/job-board/acme")


# --- pacing -----------------------------------------------------------------


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    from ats_scrapers import fetch as fetch_module

    now = [100.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(fetch_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(fetch_module.asyncio, "sleep", sleep)
    return now


def test_host_pacer_spaces_starts_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from ats_scrapers.fetch import HostPacer

    now = _fake_clock(monkeypatch)
    pacer = HostPacer(2.0)
    starts: list[tuple[str, float]] = []

    async def main() -> None:
        for host in ("a.test", "a.test", "a.test", "b.test"):
            await pacer.wait(f"https://{host}/jobs")
            starts.append((host, now[0]))

    asyncio.run(main())

    assert starts == [("a.test", 100.0), ("a.test", 100.5), ("a.test", 101.0), ("b.test", 101.0)]


def test_host_pacer_groups_hosts_by_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from ats_scrapers.fetch import HostPacer

    now = _fake_clock(monkeypatch)
    pacer = HostPacer(4.0, key=lambda host: host.split(".", 1)[1])
    starts: list[float] = []

    async def main() -> None:
        for url in (
            "https://acme.wd5.myworkdayjobs.com/x",
            "https://other.wd5.myworkdayjobs.com/x",
            "https://acme.wd1.myworkdayjobs.com/x",
        ):
            await pacer.wait(url)
            starts.append(now[0])

    asyncio.run(main())

    assert starts == [100.0, 100.25, 100.25]


def test_host_pacer_releases_late_waiters_one_interval_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ats_scrapers import fetch as fetch_module

    now = [100.0]
    monkeypatch.setattr(fetch_module.time, "monotonic", lambda: now[0])
    pacer = fetch_module.HostPacer(2.0)
    assert pacer._start_now("https://a.test/x") == 0.0
    assert [pacer._start_now("https://a.test/x") for _ in range(3)] == [0.5, 0.5, 0.5]

    # The loop was busy well past those starts: one waiter goes, the rest wait.
    now[0] = 105.0
    assert [pacer._start_now("https://a.test/x") for _ in range(3)] == [0.0, 0.5, 0.5]


def test_host_pacer_gives_up_past_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from ats_scrapers.fetch import HostPacer

    _fake_clock(monkeypatch)
    pacer = HostPacer(1.0)

    async def main() -> None:
        await pacer.wait("https://a.test/x")
        await pacer.wait("https://a.test/x", timeout=0.2)

    with pytest.raises(TimeoutError):
        asyncio.run(main())


def test_host_pacer_needs_a_positive_rate() -> None:
    from ats_scrapers.fetch import HostPacer

    with pytest.raises(ValueError, match="positive"):
        HostPacer(0)


def test_a_transport_runs_a_cloak_scraper_on_httpx() -> None:
    import httpx

    from ats_scrapers.scrapers.darwinbox import DarwinboxScraper

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    scraper = DarwinboxScraper("acme")
    scraper.transport = transport

    fetcher = scraper.make_fetcher()

    assert fetcher.engine == "httpx"
