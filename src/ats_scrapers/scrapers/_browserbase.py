"""Shared Browserbase helpers for browser-required scrapers.

Used by :mod:`ats_scrapers.scrapers.meta` and :mod:`ats_scrapers.scrapers.tesla`,
both of which can only fetch jobs through a real browser context. The
:mod:`ats_scrapers.scrapers.avature` scraper has its own inline copy of this
flow predating the shared helper; refactor target for a future cleanup.

Three env vars gate the path:

* ``BROWSERBASE_API_KEY`` + ``BROWSERBASE_PROJECT_ID`` — credentials.
* ``ATS_SCRAPERS_USE_BROWSERBASE`` (1/true/yes) — explicit opt-in for
  browser-required scrapers (meta, tesla). Without it those scrapers
  return ``[]`` with a single warning so the pipeline keeps moving.
* ``ATS_SCRAPERS_DISABLE_BROWSERBASE=1`` — emergency kill-switch shared
  with the Avature fallback.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx

from ats_scrapers.exceptions import ScraperError

log = logging.getLogger(__name__)

_TRUTHY: Final = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Return True iff the user has opted into the Browserbase path.

    Browser-required scrapers must call this *before* doing any
    Browserbase work. Returning False means "skip this scraper, do
    nothing, do not raise" — matches the Avature fallback behaviour
    when creds are absent.
    """
    if os.getenv("ATS_SCRAPERS_DISABLE_BROWSERBASE", "").lower() in _TRUTHY:
        return False
    return os.getenv("ATS_SCRAPERS_USE_BROWSERBASE", "").lower() in _TRUTHY


def require_creds() -> tuple[str, str]:
    """Return ``(api_key, project_id)`` or raise :class:`ScraperError`.

    Call this only after :func:`is_enabled` returned True — the user has
    opted in, so missing creds is a real configuration error worth
    surfacing.
    """
    api_key = os.getenv("BROWSERBASE_API_KEY")
    project_id = os.getenv("BROWSERBASE_PROJECT_ID")
    if not api_key or not project_id:
        raise ScraperError(
            "ATS_SCRAPERS_USE_BROWSERBASE is set but BROWSERBASE_API_KEY / "
            "BROWSERBASE_PROJECT_ID are missing. Either configure both "
            "or unset ATS_SCRAPERS_USE_BROWSERBASE."
        )
    return api_key, project_id


def require_playwright() -> None:
    """Raise a clear error if ``playwright`` is not importable."""
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as exc:
        raise ScraperError(
            "ATS_SCRAPERS_USE_BROWSERBASE is set but `playwright` is not "
            "installed. Run `pip install playwright` (no browser "
            "binaries needed — Browserbase runs them remotely)."
        ) from exc


async def create_session_ws_url(
    api_key: str,
    project_id: str,
    *,
    timeout: float = 30.0,
) -> str:
    """Provision a Browserbase session and return its CDP ``connectUrl``.

    Browserbase bills per minute of session time, so callers are
    expected to do all of their listing + detail work inside one
    session and close it as soon as possible.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            "https://api.browserbase.com/v1/sessions",
            headers={
                "X-BB-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "projectId": project_id,
                "browserSettings": {
                    "fingerprint": {
                        "browsers": ["chrome"],
                        "devices": ["desktop"],
                        "operatingSystems": ["macos"],
                    },
                },
            },
        )
    if response.status_code != 201:
        raise ScraperError(
            f"Browserbase session create failed: {response.status_code} "
            f"{response.text[:200]}"
        )
    return response.json()["connectUrl"]


def warn_disabled(scraper_name: str) -> None:
    """Single-line warning emitted when a browser-required scraper runs
    with ``ATS_SCRAPERS_USE_BROWSERBASE`` unset. Returns nothing so callers
    can ``return []`` after invoking it."""
    log.warning(
        "%s: browser required — set ATS_SCRAPERS_USE_BROWSERBASE=1 (with "
        "BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID configured) to "
        "enable. Skipping.",
        scraper_name,
    )
