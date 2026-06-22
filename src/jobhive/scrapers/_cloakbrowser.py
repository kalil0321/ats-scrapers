"""Shared helpers for ``cloakbrowser``-backed scrapers.

``cloakbrowser`` is a stealth-patched Chromium fork that ships its own
binary and bypasses every fingerprint check (Akamai, Cloudflare bot
manager, PerimeterX, …). Drop-in Playwright API: same ``page.goto`` /
``page.evaluate`` / ``page.mouse`` surface, just imported from
``cloakbrowser`` instead of ``playwright``. The library auto-downloads
the patched Chromium on first ``launch()`` and caches it under
``~/.cache/cloakbrowser`` — no separate ``playwright install`` step.

Two scrapers use cloakbrowser today:

  - ``tesla.py``: Akamai is aggressive on ``tesla.com``. Even
    Browserbase Sessions over residential proxies return 403/429.
    cloakbrowser + ``humanize=True`` + a behavioural warm-up clears
    the bot manager. From a datacenter IP the warm-up is still
    rate-limited so we route through the Evomi residential proxy
    when ``PROXY`` is set; on a residential machine ``PROXY`` is
    typically unset and the warm-up alone suffices.

  - ``meta.py``: GraphQL-driven SPA that needs a real browser to
    issue ``fb_dtsg`` tokens. cloakbrowser handles the cookie
    initialisation transparently; ``humanize`` is optional but
    cheap, so we keep it on for parity with the Tesla path.

Helpers below mirror the structure of :mod:`_browserbase` (which
Avature still uses as its last-resort fallback): ``is_enabled`` /
``require_cloakbrowser`` / ``warn_disabled`` / ``evomi_proxy_from_env``.
The graceful degradation contract is preserved — when ``cloakbrowser``
isn't installed, the scraper logs a warning and returns ``[]`` instead
of crashing the publish pipeline.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from jobhive.exceptions import ScraperError

log = logging.getLogger(__name__)


def is_enabled() -> bool:
    """Return True if cloakbrowser is importable.

    Treated as the on/off switch for cloakbrowser-backed scrapers —
    when the public library is installed without the ``scrapers``
    extra, this returns ``False`` and the scrapers no-op.
    """
    try:
        import cloakbrowser  # noqa: F401
    except ImportError:
        return False
    return True


def require_cloakbrowser() -> None:
    """Raise :class:`ScraperError` with a clear install hint when
    cloakbrowser isn't available. Use after :func:`is_enabled` has
    been checked (the caller decides whether the absence is fatal)."""
    try:
        import cloakbrowser  # noqa: F401
    except ImportError as exc:
        raise ScraperError(
            "cloakbrowser is required for Tesla / Meta scrapers. "
            "Install with `pip install jobhive-py[scrapers]`."
        ) from exc


def warn_disabled(scraper_name: str) -> None:
    """Log a one-line warning the operator will see in the pipeline
    log when a scraper is skipped because cloakbrowser is missing."""
    log.warning(
        "%s: browser required — install cloakbrowser "
        "(`pip install jobhive-py[scrapers]`) to enable. Skipping.",
        scraper_name,
    )


def evomi_proxy_from_env() -> dict[str, Any] | None:
    """Parse the ``PROXY`` env var into the dict shape cloakbrowser /
    Playwright accept under ``launch(proxy=...)``.

    Evomi ships ``PROXY`` in the 4-colon
    ``http://host:port:user:pass`` shape (same convention the rest of
    the codebase uses — Tesla's old patchright path, jobs.ch). Returns
    ``None`` when no env var is set so the caller passes it straight
    through without a branch.

    Used by Tesla on the datacenter VPS to avoid an immediate
    rate-limit on the first ``cua-api`` call. On a residential
    machine ``PROXY`` is typically unset and cloakbrowser warms up
    directly.
    """
    raw = os.getenv("PROXY")
    if not raw:
        return None
    rest = raw.replace("http://", "").replace("https://", "")
    parts = rest.split(":")
    if len(parts) != 4:
        log.warning(
            "PROXY env var doesn't match host:port:user:pass shape; "
            "ignoring.",
        )
        return None
    host, port, user, password = parts
    return {
        "server": f"http://{host}:{port}",
        "username": user,
        "password": password,
    }
