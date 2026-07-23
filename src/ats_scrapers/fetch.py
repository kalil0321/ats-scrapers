"""Shared HTTP fetch layer for all scrapers.

One place for the plumbing every scraper used to reimplement: retries
with exponential backoff and ``Retry-After``, status-code → exception
mapping, client lifecycle, default headers, proxy configuration, and
optional escalation to a TLS-impersonation engine when an ATS load
balancer blocks plain httpx.

Two engines behind one request-shaped interface:

- ``"httpx"`` (default): plain :class:`httpx.AsyncClient`. Right for
  the overwhelming majority of ATS APIs.
- ``"cloak"``: `httpcloak`, a TLS+h2-impersonation client whose
  fingerprint passes load balancers that 403/406-block httpx
  (Avature, JazzHR, Eightfold). Sync library, driven through
  ``asyncio.to_thread``.

Escalation is **declared, not automatic**: a scraper that knows its
ATS blocks httpx pins ``engine="cloak"``; one that only sometimes
gets blocked passes ``escalate=True`` to retry a 403/406 through
httpcloak once and stick with it for the rest of the fetcher's life.
Everything else stays on plain httpx so real errors surface instead
of being silently retried through a slower engine.

Browser-shaped fetching (cloakbrowser, Browserbase) is deliberately
*not* part of this interface — a rendered-page workflow shares no
request/response semantics with JSON APIs. See
:mod:`ats_scrapers.scrapers._browserbase` and
:mod:`ats_scrapers.scrapers._cloakbrowser` for the two scrapers that
need a real browser.

Usage::

    async with Fetcher(label="Greenhouse board acme") as fetch:
        payload = await fetch.get_json(url)

Testing: the httpx engine is mocked by ``pytest-httpx`` exactly like
direct httpx use. The module-level ``DEFAULT_RETRY_BASE_DELAY`` /
``DEFAULT_MAX_RETRY_DELAY`` are read at request time when the
constructor didn't pin values, so the test suite zeroes them once in
``conftest.py`` instead of per-scraper fixtures.
"""

from __future__ import annotations

import asyncio
import json as json_mod
import logging
import os
from types import TracebackType
from typing import Any, Literal, Self

import httpx

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError

log = logging.getLogger(__name__)

Engine = Literal["httpx", "cloak"]


class MalformedJSONError(ScraperError, ValueError):
    """A 2xx response whose body is not valid JSON (WAF/maintenance HTML).

    Subclasses both :class:`ScraperError` (the documented scraper
    exception hierarchy) and ``ValueError`` (what ``json.loads`` and
    httpx's ``.json()`` historically raised), so best-effort wrappers
    written against either contract keep working.
    """

# Total attempts per request (first try included). Module-level so the
# suite-wide conftest fixture can dial them down in one place; a
# Fetcher constructed with explicit values ignores these.
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.5
DEFAULT_MAX_RETRY_DELAY = 30.0
DEFAULT_TIMEOUT = 30.0

# Statuses that mean "try again": request timeouts, rate limits, and
# transient server errors. 403/406 are *not* here — they either
# escalate (when enabled) or fail loudly so a newly-blocking ATS is
# noticed.
_RETRYABLE_STATUSES = frozenset({408, 429}) | frozenset(range(500, 600))
_BLOCKED_STATUSES = frozenset({403, 406})


def proxy_url_from_env() -> str | None:
    """Resolve the proxy URL from the environment.

    ``ATS_SCRAPERS_PROXY`` takes a standard proxy URL
    (``http://user:pass@host:port``). The legacy ``PROXY`` var ships in
    Evomi's 4-colon ``http://host:port:user:pass`` shape (the same
    convention the browser helpers parse) and is normalized here.
    """
    url = os.getenv("ATS_SCRAPERS_PROXY")
    if url:
        return url
    raw = os.getenv("PROXY")
    if not raw:
        return None
    rest = raw.replace("http://", "").replace("https://", "")
    parts = rest.split(":")
    if len(parts) != 4:
        log.warning("PROXY env var doesn't match host:port:user:pass shape; ignoring.")
        return None
    host, port, user, password = parts
    return f"http://{user}:{password}@{host}:{port}"


class FetchResponse:
    """Engine-independent response: status, text, headers, ``.json()``.

    ``json()`` raises :class:`ScraperError` (not a bare
    ``JSONDecodeError``) on malformed bodies, so a 200 that turns out
    to be a WAF/maintenance HTML page stays inside the scraper
    exception hierarchy callers are documented to handle.
    """

    __slots__ = ("_context", "headers", "status_code", "text")

    def __init__(
        self,
        status_code: int,
        text: str,
        headers: dict[str, str],
        context: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers
        self._context = context

    def json(self) -> Any:
        try:
            return json_mod.loads(self.text)
        except ValueError as exc:
            raise MalformedJSONError(
                f"{self._context or 'fetch'}: response is not valid JSON: {exc}"
            ) from exc


class Fetcher:
    """Request-shaped fetch with retries, error mapping, and engines.

    Parameters
    ----------
    label:
        Human-readable subject for error messages, e.g.
        ``"Greenhouse board acme"`` — produces
        ``CompanyNotFoundError("Greenhouse board acme not found")``.
    engine:
        ``"httpx"`` (default) or ``"cloak"`` (httpcloak TLS
        impersonation) — pin ``"cloak"`` when the ATS is known to
        block httpx.
    escalate:
        Retry a 403/406 through httpcloak once and keep using it for
        subsequent requests on this fetcher. Requires httpcloak to be
        installed; without it the original status raises.
    retries / retry_base_delay / max_retry_delay:
        Total attempts and exponential-backoff bounds. ``None`` (the
        default) reads the module-level ``DEFAULT_*`` at request time.
    proxy:
        Proxy URL for both engines. ``None`` reads
        ``ATS_SCRAPERS_PROXY`` / legacy ``PROXY`` from the environment.
    """

    def __init__(
        self,
        *,
        label: str,
        timeout: float = DEFAULT_TIMEOUT,
        engine: Engine = "httpx",
        escalate: bool = False,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        follow_redirects: bool = True,
        retries: int | None = None,
        retry_base_delay: float | None = None,
        max_retry_delay: float | None = None,
    ) -> None:
        self.label = label
        self.timeout = timeout
        self.engine: Engine = engine
        self.escalate = escalate
        self.headers = headers or {}
        self.proxy = proxy if proxy is not None else proxy_url_from_env()
        self.follow_redirects = follow_redirects
        self._retries = retries
        self._retry_base_delay = retry_base_delay
        self._max_retry_delay = max_retry_delay
        self._client: httpx.AsyncClient | None = None

    # --- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def cookies(self) -> httpx.Cookies:
        """Cookie jar of the underlying httpx client.

        Session-flow scrapers (e.g. Phenom's CSRF dance) seed cookies
        with an initial request on this fetcher and need to read them
        back; the jar persists for the fetcher's lifetime. Only
        meaningful on the httpx engine — the cloak engine is
        stateless per request.
        """
        return self._httpx_client().cookies

    def _httpx_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=self.follow_redirects,
                headers=self.headers or None,
                proxy=self.proxy,
            )
        return self._client

    # --- public API -------------------------------------------------------

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return (await self.request("GET", url, **kwargs)).json()

    async def post_json(self, url: str, *, json: Any = None, **kwargs: Any) -> Any:
        return (await self.request("POST", url, json=json, **kwargs)).json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.request("GET", url, **kwargs)).text

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        handled: frozenset[int] | set[int] = frozenset(),
    ) -> FetchResponse:
        """Perform one logical request with retries and error mapping.

        Status handling, in order:

        - status in ``handled`` → returned to the caller unmapped
          (for provider quirks like "410 means empty board");
        - 200–299 → returned;
        - 404 → :class:`CompanyNotFoundError`;
        - 403/406 → escalate to httpcloak when enabled, else
          :class:`ScraperError`;
        - 429 / 5xx → retried with backoff (``Retry-After`` respected
          when numeric), :class:`ScraperError` after the last attempt;
        - anything else → :class:`ScraperError`.

        Network errors are retried like 5xx.
        """
        retries = self._retries if self._retries is not None else DEFAULT_RETRIES
        base_delay = (
            self._retry_base_delay
            if self._retry_base_delay is not None
            else DEFAULT_RETRY_BASE_DELAY
        )
        max_delay = (
            self._max_retry_delay
            if self._max_retry_delay is not None
            else DEFAULT_MAX_RETRY_DELAY
        )

        last_error: str = "exhausted retries"
        for attempt in range(1, retries + 1):
            try:
                response = await self._perform(
                    method, url, params=params, headers=headers, json=json
                )
            except ScraperError:
                raise
            except Exception as exc:  # network layer (httpx transport, cloak)
                last_error = str(exc)
                if attempt == retries:
                    raise ScraperError(
                        f"{self.label}: {method} {url} failed after "
                        f"{retries} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(min(base_delay * 2 ** (attempt - 1), max_delay))
                continue

            status = response.status_code
            if status in handled:
                return response
            if 200 <= status < 300:
                return response
            if status == 404:
                raise CompanyNotFoundError(f"{self.label} not found")
            if status in _BLOCKED_STATUSES and self.engine == "httpx":
                if self.escalate and _cloak_available():
                    log.info(
                        "%s: %s blocked httpx (%d) — escalating to httpcloak",
                        self.label, url, status,
                    )
                    self.engine = "cloak"
                    # Restart the logical request with a fresh attempt
                    # budget: the cloak retry is promised regardless of
                    # how many httpx attempts were already spent (e.g.
                    # retries=1 blocked on the only attempt).
                    return await self.request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                        json=json,
                        handled=handled,
                    )
                raise ScraperError(
                    f"{self.label}: {url} returned {status} — the load "
                    f"balancer is blocking plain HTTP clients"
                    + (
                        " (install httpcloak to enable escalation)"
                        if self.escalate
                        else ""
                    )
                )
            if status in _RETRYABLE_STATUSES:
                last_error = f"HTTP {status}"
                if attempt == retries:
                    raise ScraperError(
                        f"{self.label}: {url} returned {status} after "
                        f"{retries} attempts"
                    )
                retry_after = response.headers.get("Retry-After") or response.headers.get(
                    "retry-after"
                )
                delay = (
                    float(retry_after)
                    if isinstance(retry_after, str) and retry_after.isdigit()
                    else min(base_delay * 2 ** (attempt - 1), max_delay)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(f"{self.label}: {url} returned {status}")

        raise ScraperError(f"{self.label}: {method} {url} failed: {last_error}")

    # --- engines ----------------------------------------------------------

    async def _perform(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        json: Any,
    ) -> FetchResponse:
        if self.engine == "cloak":
            return await asyncio.to_thread(
                self._cloak_request_sync, method, url, params, headers, json
            )
        response = await self._httpx_client().request(
            method, url, params=params, headers=headers, json=json
        )
        return FetchResponse(
            response.status_code,
            response.text,
            dict(response.headers),
            context=f"{self.label}: {url}",
        )

    def _cloak_request_sync(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        json: Any,
    ) -> FetchResponse:
        import httpcloak  # type: ignore[import-untyped]

        merged_headers = {**self.headers, **(headers or {})}
        kwargs: dict[str, Any] = {
            "params": params,
            "headers": merged_headers or None,
            # Fetcher exposes seconds like httpx; httpcloak expects ms.
            "timeout": int(self.timeout * 1000),
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if method.upper() == "GET":
            response = httpcloak.get(url, **kwargs)
        elif method.upper() == "POST":
            response = httpcloak.post(url, json=json, **kwargs)
        else:
            raise ScraperError(
                f"{self.label}: cloak engine does not support {method}"
            )
        return FetchResponse(
            response.status_code,
            response.text,
            dict(getattr(response, "headers", {}) or {}),
            context=f"{self.label}: {url}",
        )


def _cloak_available() -> bool:
    try:
        import httpcloak  # noqa: F401
    except ImportError:
        return False
    return True
