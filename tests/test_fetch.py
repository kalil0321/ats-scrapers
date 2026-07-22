"""Tests for the shared HTTP fetch layer (`ats_scrapers.fetch`)."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.fetch import Fetcher, FetchResponse, proxy_url_from_env

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


async def test_network_errors_retry(httpx_mock) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("boom"))
    httpx_mock.add_response(url=URL, json={"ok": 1})
    async with _fetcher() as fetch:
        assert await fetch.get_json(URL) == {"ok": 1}


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
    def __init__(self, status_code: int = 200, text: str = "{}") -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}


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
    async with _fetcher(escalate=True) as fetch:
        payload = await fetch.get_json(URL)
    assert payload == {"jobs": []}
    assert calls and calls[0]["url"] == URL
    # The fetcher stays escalated: the next request goes straight to cloak.
    async with asyncio.timeout(5):
        await fetch.get_json(URL)
    assert len(calls) == 2


async def test_escalation_gets_fresh_attempt_budget(httpx_mock, monkeypatch) -> None:
    """A 403 on the FINAL httpx attempt (retries=1) must still escalate."""
    calls: list[dict[str, Any]] = []
    _install_fake_httpcloak(monkeypatch, calls, _FakeCloakResponse(text='{"ok": 1}'))
    httpx_mock.add_response(url=URL, status_code=403)
    async with _fetcher(escalate=True, retries=1) as fetch:
        assert await fetch.get_json(URL) == {"ok": 1}
    assert len(calls) == 1


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


# --- proxy config -----------------------------------------------------------


def test_proxy_env_standard_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATS_SCRAPERS_PROXY", "http://user:pass@proxy.example:8080")
    assert proxy_url_from_env() == "http://user:pass@proxy.example:8080"


def test_proxy_env_legacy_evomi_shape(monkeypatch: pytest.MonkeyPatch) -> None:
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
