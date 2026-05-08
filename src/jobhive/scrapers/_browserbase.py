"""Shared Browserbase helpers for browser-required scrapers.

Used by :mod:`jobhive.scrapers.meta` and :mod:`jobhive.scrapers.tesla`,
both of which can only fetch jobs through a real browser context. The
:mod:`jobhive.scrapers.avature` scraper has its own inline copy of this
flow predating the shared helper; refactor target for a future cleanup.

Three env vars gate the path:

* ``BROWSERBASE_API_KEY`` + ``BROWSERBASE_PROJECT_ID`` — credentials.
* ``JOBHIVE_USE_BROWSERBASE`` (1/true/yes) — explicit opt-in for
  browser-required scrapers (meta, tesla). Without it those scrapers
  return ``[]`` with a single warning so the pipeline keeps moving.
* ``JOBHIVE_DISABLE_BROWSERBASE=1`` — emergency kill-switch shared
  with the Avature fallback.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx

from jobhive.exceptions import ScraperError

log = logging.getLogger(__name__)

_TRUTHY: Final = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Return True iff the user has opted into the Browserbase path.

    Browser-required scrapers must call this *before* doing any
    Browserbase work. Returning False means "skip this scraper, do
    nothing, do not raise" — matches the Avature fallback behaviour
    when creds are absent.
    """
    if os.getenv("JOBHIVE_DISABLE_BROWSERBASE", "").lower() in _TRUTHY:
        return False
    return os.getenv("JOBHIVE_USE_BROWSERBASE", "").lower() in _TRUTHY


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
            "JOBHIVE_USE_BROWSERBASE is set but BROWSERBASE_API_KEY / "
            "BROWSERBASE_PROJECT_ID are missing. Either configure both "
            "or unset JOBHIVE_USE_BROWSERBASE."
        )
    return api_key, project_id


def require_playwright() -> None:
    """Raise a clear error if ``playwright`` is not importable."""
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as exc:
        raise ScraperError(
            "JOBHIVE_USE_BROWSERBASE is set but `playwright` is not "
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
    with ``JOBHIVE_USE_BROWSERBASE`` unset. Returns nothing so callers
    can ``return []`` after invoking it."""
    log.warning(
        "%s: browser required — set JOBHIVE_USE_BROWSERBASE=1 (with "
        "BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID configured) to "
        "enable. Skipping.",
        scraper_name,
    )


# --- macOS focus-restore for patchright launches ---------------------------
#
# patchright runs HEADED Chromium (Akamai detects headless even with
# stealth patches). On macOS, launching a foreground GUI app steals
# the keyboard focus from whatever the user is doing — annoying when
# the cron fires while they're typing. Mitigation: capture the
# frontmost app BEFORE the launch, re-activate it AFTER. The window
# is also `--start-minimized` and positioned off-screen so it never
# shows up visually.

import contextlib  # noqa: E402  — kept near its only callers
import subprocess  # noqa: E402
import sys  # noqa: E402


def capture_frontmost_app_macos() -> str | None:
    """Return the name of the frontmost macOS app, or ``None`` on
    non-Darwin / on any subprocess failure. Best-effort; never
    raises."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to name of '
                "first process whose frontmost is true",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    name = (result.stdout or "").strip()
    return name or None


def reactivate_app_macos(name: str | None) -> None:
    """Re-bring ``name`` to the foreground via AppleScript. No-op when
    name is None or on non-Darwin. Best-effort; never raises."""
    if not name or sys.platform != "darwin":
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["osascript", "-e", f'tell application "{name}" to activate'],
            timeout=2,
            check=False,
            capture_output=True,
        )


# Patchright launch args used by Tesla + Meta (both browser-required
# scrapers). Headed because Akamai detects headless; minimized +
# off-screen so the operator never sees the window.
PATCHRIGHT_INVISIBLE_ARGS: tuple[str, ...] = (
    "--start-minimized",
    "--window-position=-32000,-32000",
    "--window-size=1440,900",
)
