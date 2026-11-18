"""Hosted browsers: session APIs are mocked with pytest-httpx and the CDP
connection with fakes, so nothing here reaches a provider."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from ats_scrapers import CloudBrowser, RenderedPage, ScraperError
from ats_scrapers import cloud_browser as cb
from ats_scrapers.cloud_browser import (
    BrowserbaseBrowser,
    BrowserUseBrowser,
    CDPCloudBrowser,
    KernelBrowser,
    NotteBrowser,
)

CDP_URL = "wss://connect.example/s1"


@dataclass
class Provider:
    cls: type[CDPCloudBrowser]
    env_var: str
    auth: tuple[str, str]  # header and its value for the key "key"
    start: tuple[str, str, int, dict[str, Any]]  # method, URL, status, response
    stop: tuple[str, str, int, dict[str, Any] | None]  # method, URL, status, body sent


PROVIDERS = [
    pytest.param(Provider(
        BrowserbaseBrowser, "BROWSERBASE_API_KEY", ("X-BB-API-Key", "key"),
        ("POST", "https://api.browserbase.com/v1/sessions", 201,
         {"id": "s1", "connectUrl": CDP_URL}),
        ("POST", "https://api.browserbase.com/v1/sessions/s1", 200,
         {"status": "REQUEST_RELEASE"}),
    ), id="browserbase"),
    pytest.param(Provider(
        BrowserUseBrowser, "BROWSER_USE_API_KEY", ("X-Browser-Use-API-Key", "key"),
        ("POST", "https://api.browser-use.com/api/v4/browsers", 201,
         {"id": "s1", "cdpUrl": CDP_URL}),
        ("PATCH", "https://api.browser-use.com/api/v4/browsers/s1", 200, {"action": "stop"}),
    ), id="browser-use"),
    pytest.param(Provider(
        KernelBrowser, "KERNEL_API_KEY", ("Authorization", "Bearer key"),
        ("POST", "https://api.onkernel.com/browsers", 200,
         {"session_id": "s1", "cdp_ws_url": CDP_URL}),
        ("DELETE", "https://api.onkernel.com/browsers/s1", 204, None),
    ), id="kernel"),
    pytest.param(Provider(
        NotteBrowser, "NOTTE_API_KEY", ("Authorization", "Bearer key"),
        ("POST", "https://api.notte.cc/sessions/start", 200,
         {"session_id": "s1", "cdp_url": CDP_URL}),
        ("DELETE", "https://api.notte.cc/sessions/s1/stop", 200, None),
    ), id="notte"),
]


def _mock_sessions(httpx_mock, provider: Provider, *, reusable: bool = False) -> None:
    method, url, status, payload = provider.start
    httpx_mock.add_response(
        method=method, url=url, status_code=status, json=payload, is_reusable=reusable,
    )
    method, url, status, _ = provider.stop
    httpx_mock.add_response(
        method=method, url=url, status_code=status, is_reusable=reusable,
        **({} if status == 204 else {"json": {}}),
    )


def _body(request) -> Any:
    return json.loads(request.content) if request.content else None


@dataclass
class FakeCDP:
    """Stands in for Playwright connected over CDP."""

    status: int = 200
    redirects: dict[str, str] = field(default_factory=dict)
    connections: list[SimpleNamespace] = field(default_factory=list)
    fail: bool = False

    async def connect(self, cdp_url: str) -> tuple[Any, Any]:
        if self.fail:
            raise RuntimeError("CDP handshake refused")
        cdp = self

        class Page:
            url = "about:blank"
            closed = False

            async def goto(self, url: str, **_: Any) -> SimpleNamespace:
                self.url = cdp.redirects.get(url, url)
                return SimpleNamespace(status=cdp.status)

            async def content(self) -> str:
                return f"<html>{self.url}</html>"

            async def close(self) -> None:
                self.closed = True

        connection = SimpleNamespace(
            url=cdp_url, pages=[], connected=True, closed=False, stopped=False,
        )

        async def new_page() -> Page:
            await asyncio.sleep(0)
            connection.pages.append(Page())
            return connection.pages[-1]

        async def close() -> None:
            connection.closed, connection.connected = True, False

        async def stop() -> None:
            connection.stopped = True

        browser = SimpleNamespace(
            contexts=[SimpleNamespace(new_page=new_page)],
            is_connected=lambda: connection.connected,
            close=close,
        )
        self.connections.append(connection)
        return SimpleNamespace(stop=stop), browser


@pytest.fixture
def cdp(monkeypatch: pytest.MonkeyPatch) -> FakeCDP:
    fake = FakeCDP()
    monkeypatch.setattr(cb, "_connect_over_cdp", fake.connect)
    return fake


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_one_session_serves_every_render_until_closed(
    provider: Provider, httpx_mock, cdp: FakeCDP,
) -> None:
    _mock_sessions(httpx_mock, provider)
    cdp.redirects["https://acme.example/jobs"] = "https://acme.example/en/jobs"

    async with provider.cls(api_key="key") as browser:
        first = await browser.render("https://acme.example/jobs")
        second = await browser.render("https://acme.example/jobs/1")

    assert first == RenderedPage(
        html="<html>https://acme.example/en/jobs</html>", url="https://acme.example/en/jobs",
    )
    assert second.url == "https://acme.example/jobs/1"
    start, stop = httpx_mock.get_requests()
    assert (start.method, str(start.url)) == provider.start[:2]
    assert start.headers[provider.auth[0]] == provider.auth[1]
    assert (stop.method, str(stop.url), _body(stop)) == (
        provider.stop[0], provider.stop[1], provider.stop[3],
    )
    assert stop.headers[provider.auth[0]] == provider.auth[1]
    [connection] = cdp.connections
    assert connection.url == CDP_URL
    assert all(page.closed for page in connection.pages)
    assert connection.closed and connection.stopped


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_api_key_is_read_from_the_environment(
    provider: Provider, httpx_mock, cdp: FakeCDP, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(provider.env_var, "key")
    _mock_sessions(httpx_mock, provider)

    async with provider.cls() as browser:
        await browser.render("https://acme.example/jobs")

    assert httpx_mock.get_requests()[0].headers[provider.auth[0]] == provider.auth[1]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_missing_api_key_raises(provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(provider.env_var, raising=False)

    with pytest.raises(ValueError, match=provider.env_var):
        provider.cls()


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_session_options_are_the_start_request_body(
    provider: Provider, httpx_mock, cdp: FakeCDP, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)
    _mock_sessions(httpx_mock, provider)

    async with provider.cls("key", session_options={"timeout": 5}) as browser:
        await browser.render("https://acme.example/jobs")

    assert _body(httpx_mock.get_requests()[0]) == {"timeout": 5}


async def test_browserbase_sends_the_project(
    httpx_mock, cdp: FakeCDP, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "p1")
    _mock_sessions(httpx_mock, PROVIDERS[0].values[0])

    async with BrowserbaseBrowser("key") as browser:
        await browser.render("https://acme.example/jobs")

    assert _body(httpx_mock.get_requests()[0]) == {"projectId": "p1"}


async def test_failed_session_start_raises_without_the_key(httpx_mock, cdp: FakeCDP) -> None:
    httpx_mock.add_response(
        method="POST", url="https://api.browserbase.com/v1/sessions", status_code=402,
        text="Payment required",
    )

    async with BrowserbaseBrowser("secret-key") as browser:
        with pytest.raises(ScraperError, match="Browserbase session start failed: 402") as exc:
            await browser.render("https://acme.example/jobs")

    assert "secret-key" not in str(exc.value)
    assert cdp.connections == []


async def test_concurrent_renders_share_one_session(httpx_mock, cdp: FakeCDP) -> None:
    _mock_sessions(httpx_mock, PROVIDERS[1].values[0])

    async with BrowserUseBrowser("key") as browser:
        pages = await asyncio.gather(*(
            browser.render(f"https://acme.example/jobs/{i}") for i in range(4)
        ))

    assert [p.url for p in pages] == [f"https://acme.example/jobs/{i}" for i in range(4)]
    assert [r.method for r in httpx_mock.get_requests()] == ["POST", "PATCH"]
    assert len(cdp.connections) == 1


async def test_page_with_an_error_status_raises(httpx_mock, cdp: FakeCDP) -> None:
    _mock_sessions(httpx_mock, PROVIDERS[2].values[0])
    cdp.status = 406

    async with KernelBrowser("key") as browser:
        with pytest.raises(ScraperError, match="answered 406"):
            await browser.render("https://acme.example/jobs")

    assert cdp.connections[0].pages[0].closed


async def test_a_session_the_service_ended_is_replaced(httpx_mock, cdp: FakeCDP) -> None:
    _mock_sessions(httpx_mock, PROVIDERS[3].values[0], reusable=True)

    async with NotteBrowser("key") as browser:
        await browser.render("https://acme.example/jobs")
        cdp.connections[0].connected = False
        await browser.render("https://acme.example/jobs")

    assert [r.method for r in httpx_mock.get_requests()] == ["POST", "DELETE", "POST", "DELETE"]
    assert len(cdp.connections) == 2


async def test_failed_cdp_connection_stops_the_session(httpx_mock, cdp: FakeCDP) -> None:
    _mock_sessions(httpx_mock, PROVIDERS[0].values[0])
    cdp.fail = True

    async with BrowserbaseBrowser("key") as browser:
        with pytest.raises(RuntimeError, match="CDP handshake refused"):
            await browser.render("https://acme.example/jobs")

    assert [r.method for r in httpx_mock.get_requests()] == ["POST", "POST"]



async def test_a_failed_stop_is_retried_on_the_next_close(httpx_mock, cdp: FakeCDP) -> None:
    provider = PROVIDERS[3].values[0]
    method, url, status, payload = provider.start
    httpx_mock.add_response(method=method, url=url, status_code=status, json=payload)
    stop_method, stop_url, _, _ = provider.stop
    httpx_mock.add_response(method=stop_method, url=stop_url, status_code=503)
    httpx_mock.add_response(method=stop_method, url=stop_url, json={})
    browser = NotteBrowser("key")
    await browser.render("https://acme.example/jobs")

    with pytest.raises(ScraperError, match="503"):
        await browser.aclose()
    await browser.aclose()

    assert [r.method for r in httpx_mock.get_requests()] == ["POST", "DELETE", "DELETE"]


async def test_closing_during_a_session_start_stops_that_session(
    httpx_mock, cdp: FakeCDP, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_sessions(httpx_mock, PROVIDERS[3].values[0])
    browser = NotteBrowser("key")
    started = asyncio.Event()
    start = browser.start_session
    loop = asyncio.get_running_loop()

    async def slow_start(client):
        loop.call_soon_threadsafe(started.set)
        await asyncio.sleep(0.2)
        return await start(client)

    monkeypatch.setattr(browser, "start_session", slow_start)
    render = asyncio.ensure_future(browser.render("https://acme.example/jobs"))
    await started.wait()

    await browser.aclose()
    # Bounded so a render stranded by close fails the test instead of hanging it.
    await asyncio.wait_for(asyncio.gather(render, return_exceptions=True), timeout=5)

    assert [r.method for r in httpx_mock.get_requests()] == ["POST", "DELETE"]

def test_one_browser_serves_sync_calls_across_event_loops(httpx_mock, cdp: FakeCDP) -> None:
    _mock_sessions(httpx_mock, PROVIDERS[0].values[0])

    with BrowserbaseBrowser("key") as browser:
        asyncio.run(browser.render("https://acme.example/jobs"))
        asyncio.run(browser.render("https://acme.example/jobs/1"))

    assert [r.method for r in httpx_mock.get_requests()] == ["POST", "POST"]
    assert len(cdp.connections) == 1


def test_a_browser_that_never_rendered_starts_no_session(httpx_mock) -> None:
    with BrowserbaseBrowser("key"):
        pass

    assert httpx_mock.get_requests() == []


def test_missing_playwright_raises_on_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cb, "_has_playwright", lambda: False)

    with pytest.raises(ImportError, match=r"ats-scrapers\[cloud-browser\]"):
        KernelBrowser("key")


def test_playwright_is_not_imported_with_the_package() -> None:
    code = (
        "import sys, ats_scrapers, ats_scrapers.cloud_browser, ats_scrapers.scrapers; "
        "sys.exit('playwright' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_a_cloud_browser_must_implement_render() -> None:
    class NoRender(CloudBrowser):
        pass

    with pytest.raises(TypeError, match="render"):
        NoRender()  # type: ignore[abstract]
