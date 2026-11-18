"""Remote browsers that scrapers can be given for pages plain HTTP cannot load.

:class:`CloudBrowser` is what a scraper calls. :class:`BrowserbaseBrowser`,
:class:`BrowserUseBrowser`, :class:`KernelBrowser` and :class:`NotteBrowser`
rent a browser from those services with your own API key; they need the
``cloud-browser`` extra (Playwright).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Coroutine, Mapping
from concurrent.futures import Future
from typing import Any, NamedTuple, Self, TypeVar

import httpx

from ats_scrapers.exceptions import ScraperError

T = TypeVar("T")


class RenderedPage(NamedTuple):
    """A page as a browser rendered it."""

    html: str
    #: The address that answered, after redirects.
    url: str


class CloudBrowser(ABC):
    """Loads pages in a real browser for scrapers that accept ``cloud_browser``.

    Subclass it to wrap the browser service you use. ``render`` may be called
    for several pages at once and should raise when a page cannot be loaded.
    Scrapers never open or close the browser: close it yourself, with
    ``aclose()`` or an ``async with`` block.
    """

    @abstractmethod
    async def render(self, url: str) -> RenderedPage:
        """Load ``url`` and return its HTML once the document has loaded."""

    async def aclose(self) -> None:
        """Release what the browser holds. There is nothing to release by default."""
        return

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class CDPCloudBrowser(CloudBrowser):
    """A hosted browser reached over the Chrome DevTools Protocol.

    The first ``render`` starts a remote session and later ones reuse it until
    the browser is closed; a session the service ended is replaced. Services
    bill while a session is open, so close the browser when done. Playwright
    runs on its own thread, so one browser serves sync ``fetch()`` calls and
    any event loop. Subclasses start and stop sessions on the service's API;
    ``session_options`` is the body of the start request.
    """

    #: Name used in error messages.
    provider = "cloud browser"

    def __init__(
        self,
        *,
        session_options: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not _has_playwright():
            raise ImportError(
                f"{type(self).__name__} needs Playwright: "
                "pip install 'ats-scrapers[cloud-browser]'"
            )
        self.session_options = dict(session_options or {})
        self.timeout = timeout
        self._worker: tuple[asyncio.AbstractEventLoop, threading.Thread] | None = None
        self._worker_lock = threading.Lock()
        self._closing = False
        self._session_lock: asyncio.Lock | None = None
        self._session_id: str | None = None
        self._playwright: Any = None
        self._browser: Any = None

    @abstractmethod
    async def start_session(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """Start a remote browser and return ``(session_id, cdp_url)``."""

    @abstractmethod
    async def stop_session(self, client: httpx.AsyncClient, session_id: str) -> None:
        """Stop the remote browser ``session_id``."""

    async def render(self, url: str) -> RenderedPage:
        return await asyncio.wrap_future(self._submit(self._render(url)))

    async def aclose(self) -> None:
        worker = self._take_worker()
        try:
            if worker is not None:
                await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(self._end_session(), worker[0])
                )
        finally:
            self._closed(worker)

    def close(self) -> None:
        """Synchronous :meth:`aclose`."""
        worker = self._take_worker()
        try:
            if worker is not None:
                asyncio.run_coroutine_threadsafe(self._end_session(), worker[0]).result()
        finally:
            self._closed(worker)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _expect(self, response: httpx.Response, action: str) -> dict[str, Any]:
        if not response.is_success:
            raise ScraperError(
                f"{self.provider} {action} failed: "
                f"{response.status_code} {response.text[:200]}"
            )
        return response.json() if response.content else {}

    def _field(self, data: Mapping[str, Any], name: str) -> str:
        value = data.get(name)
        if not value:
            raise ScraperError(f"{self.provider} returned a session without {name!r}")
        return str(value)

    def _submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        try:
            loop = self._ensure_worker()
        except ScraperError:
            coro.close()
            raise
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _ensure_worker(self, *, closing: bool = False) -> asyncio.AbstractEventLoop:
        with self._worker_lock:
            # A render during close would get a loop of its own and wait forever
            # on a session lock held by the closing one.
            if self._closing and not closing:
                raise ScraperError(f"{self.provider}: the browser is closing")
            if self._worker is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name=type(self).__name__, daemon=True,
                )
                thread.start()
                self._worker = (loop, thread)
            return self._worker[0]

    def _take_worker(self) -> tuple[asyncio.AbstractEventLoop, threading.Thread] | None:
        with self._worker_lock:
            self._closing = True
        # A session whose stop failed still needs a loop to release it.
        if self._session_id is not None:
            self._ensure_worker(closing=True)
        with self._worker_lock:
            worker, self._worker = self._worker, None
        return worker

    def _closed(self, worker: tuple[asyncio.AbstractEventLoop, threading.Thread] | None) -> None:
        if worker is not None:
            _stop_worker(*worker)
        self._session_lock = None
        with self._worker_lock:
            self._closing = False

    # Everything below runs on the worker thread's loop.

    async def _render(self, url: str) -> RenderedPage:
        browser = await self._connected_browser()
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout * 1000,
            )
            if response is not None and response.status >= 400:
                raise ScraperError(f"{self.provider}: {url} answered {response.status}")
            return RenderedPage(html=await _settled_html(page, self.timeout), url=page.url)
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    async def _connected_browser(self) -> Any:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._browser is not None and not self._browser.is_connected():
                await self._end_session_now()
            if self._browser is None:
                if self._session_id is not None:
                    # Release a session whose stop failed before renting another.
                    await self._end_session_now()
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    self._session_id, cdp_url = await self.start_session(client)
                try:
                    self._playwright, self._browser = await _connect_over_cdp(cdp_url)
                except BaseException:
                    await self._end_session_now()
                    raise
            return self._browser

    async def _end_session(self) -> None:
        # Waits for a session being started, so closing never strands it.
        if self._session_lock is None:
            await self._end_session_now()
            return
        async with self._session_lock:
            await self._end_session_now()

    async def _end_session_now(self) -> None:
        playwright, browser = self._playwright, self._browser
        self._playwright = self._browser = None
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()
        if self._session_id is not None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await self.stop_session(client, self._session_id)
            # Kept until the provider confirms, so a later close can retry.
            self._session_id = None


class BrowserbaseBrowser(CDPCloudBrowser):
    """Sessions on `Browserbase <https://www.browserbase.com>`_.

    ``api_key`` defaults to ``BROWSERBASE_API_KEY`` and ``project_id`` to
    ``BROWSERBASE_PROJECT_ID``; without a project, Browserbase uses the key's.
    """

    provider = "Browserbase"
    api_url = "https://api.browserbase.com/v1/sessions"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project_id: str | None = None,
        session_options: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(session_options=session_options, timeout=timeout)
        key = _api_key(api_key, "BROWSERBASE_API_KEY", type(self).__name__)
        self._headers = {"X-BB-API-Key": key}
        self.project_id = project_id or os.getenv("BROWSERBASE_PROJECT_ID")

    async def start_session(self, client: httpx.AsyncClient) -> tuple[str, str]:
        body = dict(self.session_options)
        if self.project_id:
            body.setdefault("projectId", self.project_id)
        response = await client.post(self.api_url, headers=self._headers, json=body)
        data = self._expect(response, "session start")
        return self._field(data, "id"), self._field(data, "connectUrl")

    async def stop_session(self, client: httpx.AsyncClient, session_id: str) -> None:
        response = await client.post(
            f"{self.api_url}/{session_id}",
            headers=self._headers,
            json={"status": "REQUEST_RELEASE"},
        )
        self._expect(response, "session stop")


class BrowserUseBrowser(CDPCloudBrowser):
    """Browser sessions on `Browser Use <https://browser-use.com>`_ Cloud.

    ``api_key`` defaults to ``BROWSER_USE_API_KEY``.
    """

    provider = "Browser Use"
    api_url = "https://api.browser-use.com/api/v4/browsers"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        session_options: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(session_options=session_options, timeout=timeout)
        key = _api_key(api_key, "BROWSER_USE_API_KEY", type(self).__name__)
        self._headers = {"X-Browser-Use-API-Key": key}

    async def start_session(self, client: httpx.AsyncClient) -> tuple[str, str]:
        response = await client.post(
            self.api_url, headers=self._headers, json=self.session_options,
        )
        data = self._expect(response, "session start")
        return self._field(data, "id"), self._field(data, "cdpUrl")

    async def stop_session(self, client: httpx.AsyncClient, session_id: str) -> None:
        response = await client.patch(
            f"{self.api_url}/{session_id}", headers=self._headers, json={"action": "stop"},
        )
        self._expect(response, "session stop")


class KernelBrowser(CDPCloudBrowser):
    """Browsers on `Kernel <https://www.kernel.sh>`_.

    ``api_key`` defaults to ``KERNEL_API_KEY``.
    """

    provider = "Kernel"
    api_url = "https://api.onkernel.com/browsers"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        session_options: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(session_options=session_options, timeout=timeout)
        key = _api_key(api_key, "KERNEL_API_KEY", type(self).__name__)
        self._headers = {"Authorization": f"Bearer {key}"}

    async def start_session(self, client: httpx.AsyncClient) -> tuple[str, str]:
        response = await client.post(
            self.api_url, headers=self._headers, json=self.session_options,
        )
        data = self._expect(response, "session start")
        return self._field(data, "session_id"), self._field(data, "cdp_ws_url")

    async def stop_session(self, client: httpx.AsyncClient, session_id: str) -> None:
        response = await client.delete(f"{self.api_url}/{session_id}", headers=self._headers)
        self._expect(response, "session stop")


class NotteBrowser(CDPCloudBrowser):
    """Sessions on `Notte <https://www.notte.cc>`_.

    ``api_key`` defaults to ``NOTTE_API_KEY``.
    """

    provider = "Notte"
    api_url = "https://api.notte.cc/sessions"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        session_options: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(session_options=session_options, timeout=timeout)
        key = _api_key(api_key, "NOTTE_API_KEY", type(self).__name__)
        self._headers = {"Authorization": f"Bearer {key}"}

    async def start_session(self, client: httpx.AsyncClient) -> tuple[str, str]:
        response = await client.post(
            f"{self.api_url}/start", headers=self._headers, json=self.session_options,
        )
        data = self._expect(response, "session start")
        return self._field(data, "session_id"), self._field(data, "cdp_url")

    async def stop_session(self, client: httpx.AsyncClient, session_id: str) -> None:
        response = await client.delete(
            f"{self.api_url}/{session_id}/stop", headers=self._headers,
        )
        self._expect(response, "session stop")


def _api_key(value: str | None, env_var: str, owner: str) -> str:
    key = value or os.getenv(env_var)
    if not key:
        raise ValueError(f"{owner} needs an API key: pass api_key or set {env_var}")
    return key


async def _settled_html(page: Any, timeout: float) -> str:
    """The page's HTML once it has stopped moving on by itself.

    A JavaScript challenge (AWS WAF, Cloudflare) loads, solves itself and then
    loads the real page: read in between, the HTML is the challenge's, or
    Playwright refuses because the page is navigating.
    """
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state(
                "networkidle", timeout=max(deadline - time.monotonic(), 0.1) * 1000,
            )
        try:
            return await page.content()
        except Exception as exc:
            if "navigating" not in str(exc) or time.monotonic() >= deadline:
                raise
        await asyncio.sleep(0.25)


def _has_playwright() -> bool:
    return importlib.util.find_spec("playwright") is not None


async def _connect_over_cdp(cdp_url: str) -> tuple[Any, Any]:
    """``(playwright, browser)`` connected to ``cdp_url``."""
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        return playwright, await playwright.chromium.connect_over_cdp(cdp_url)
    except BaseException:
        await playwright.stop()
        raise


def _stop_worker(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()
