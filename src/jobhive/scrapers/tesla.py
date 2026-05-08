"""Tesla careers scraper — patchright-backed.

Tesla's public listings live behind ``/cua-api/apps/careers/state``,
which returns the entire job catalog as one JSON document. Direct
``httpx`` calls are 403'd by Akamai bot detection — a real browser is
required, and Akamai's behavioral fingerprinting on the
``/cua-api/*`` endpoints is aggressive enough to block plain
Playwright (even with stealth args + ``playwright-stealth``) AND
default Browserbase Sessions (with or without residential proxies).

The bypass we ship is `patchright`_ — an open-source Playwright fork
with deeper stealth patches baked into the bundled Chromium. It
clears the Akamai challenge cleanly on a residential IP. Trade-off:
patchright runs locally (downloads its own Chromium), so this path
only works when the host has the binary installed
(``patchright install chromium``). For environments where that's not
viable, see the install notes in the docstring.

.. _patchright: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright

Activation:

- Set ``JOBHIVE_USE_BROWSERBASE=1`` (the umbrella opt-in for
  browser-required scrapers — kept the same name as Meta's flag for
  consistency, even though Tesla doesn't use Browserbase).
- Install patchright + its bundled Chromium:
  ``pip install jobhive-py[browser]`` then
  ``patchright install chromium``.

Without the flag, ``fetch()`` returns ``[]`` with a single warning
so the rest of the pipeline keeps moving. Without patchright but
with the flag set, raises a clear ``ScraperError``.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers import _browserbase as bb
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

_BASE_URL = "https://www.tesla.com"
_CAREERS_HOME = "/careers/search/jobs"
_STATE_ENDPOINT = "/cua-api/apps/careers/state"

# Match the JSON body whether the browser wraps it in <pre>…</pre> or
# inlines it as plain text — both forms appear depending on browser /
# user-agent.
_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL)


@ScraperRegistry.register(ATSType.TESLA)
class TeslaScraper(BaseScraper):
    """Tesla scraper. Single tenant — slug is ignored."""

    ats = ATSType.TESLA

    def fetch(self) -> list[Job]:
        if not bb.is_enabled():
            bb.warn_disabled("Tesla")
            return []
        return asyncio.run(self._fetch_via_patchright())

    async def _fetch_via_patchright(self) -> list[Job]:
        try:
            from patchright.async_api import async_playwright
        except ImportError as exc:
            raise ScraperError(
                "Tesla requires `patchright` to bypass Akamai. Install "
                "with `pip install jobhive-py[browser]`, then run "
                "`patchright install chromium` to download its bundled "
                "Chromium build."
            ) from exc

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=False)
            except Exception as exc:
                raise ScraperError(
                    f"Tesla: patchright Chromium launch failed ({exc}). "
                    "Did you run `patchright install chromium`?"
                ) from exc
            try:
                ctx = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                )
                page = await ctx.new_page()

                # Warm up Akamai cookies by visiting the careers page.
                # ``networkidle`` lets Akamai's challenge JS finish
                # gathering its signals (canvas, WebGL, plugin probe,
                # …) and submit them. patchright's masked Chromium
                # passes the resulting bot score; default Playwright
                # doesn't.
                await page.goto(
                    f"{_BASE_URL}{_CAREERS_HOME}",
                    wait_until="networkidle",
                    timeout=60_000,
                )

                # Sanity check before the API call: if the warmup
                # produced an Access Denied page, fail fast with a
                # diagnostic the operator can act on (proxy issue,
                # patchright outdated vs new Akamai rules, …).
                if "access denied" in (await page.content()).lower():
                    raise ScraperError(
                        "Tesla: warmup page is 'Access Denied'. "
                        "patchright did not pass Akamai — try "
                        "upgrading patchright or running with a "
                        "different residential IP."
                    )

                # Hit the JSON endpoint inside the same browser
                # session — same cookies, same Akamai-validated bot
                # score.
                resp = await page.goto(
                    f"{_BASE_URL}{_STATE_ENDPOINT}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                if resp is None or resp.status != 200:
                    raise ScraperError(
                        f"Tesla: /cua-api/state returned status="
                        f"{resp.status if resp else 'None'} after warmup."
                    )
                payload = await self._extract_json(page)
            finally:
                await browser.close()

        return list(self._parse_payload(payload))

    @staticmethod
    async def _extract_json(page: Any) -> dict[str, Any]:
        html = await page.content()
        match = _PRE_RE.search(html)
        body = match.group(1) if match else await page.inner_text("body")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ScraperError(
                f"Tesla: response did not parse as JSON ({exc})."
            ) from exc

    def _parse_payload(self, payload: dict[str, Any]) -> list[Job]:
        listings = payload.get("listings") or []
        locations = (payload.get("lookup") or {}).get("locations") or {}
        departments = (payload.get("lookup") or {}).get("departments") or {}
        fetched_at = datetime.now(tz=UTC)
        jobs: list[Job] = []
        for entry in listings:
            job_id = entry.get("id") or entry.get("ji")
            title = entry.get("t") or entry.get("title")
            if not job_id or not title:
                continue
            location = locations.get(entry.get("l"))
            department_id = entry.get("d")
            department = departments.get(department_id) if department_id else None
            slug = self._url_slug(title, str(job_id))
            url = f"{_BASE_URL}/careers/search/job/{slug}"
            jobs.append(
                Job(
                    url=url,
                    title=title,
                    company="Tesla",
                    ats_type=ATSType.TESLA,
                    ats_id=str(job_id),
                    location=location,
                    department=department,
                    fetched_at=fetched_at,
                    raw=entry,
                )
            )
        return jobs

    @staticmethod
    def _url_slug(title: str, job_id: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return f"{slug}-{job_id}" if slug else job_id
