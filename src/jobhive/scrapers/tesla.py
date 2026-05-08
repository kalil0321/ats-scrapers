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

        # patchright clears Akamai only in HEADED mode — the headless
        # variant slips a fingerprint signal Tesla's detector catches.
        # The cron is expected to run overnight when no operator is
        # at the keyboard, so we don't bother hiding the window.
        #
        # Datacenter-IP runs (Hetzner / AWS / etc.) get blocked by
        # Akamai's IP reputation gate before we can even render the
        # careers page; the workaround is to route through a
        # residential proxy. Set ``PROXY=http://host:port:user:pass``
        # in the env (4-colon Evomi format) and we plug it into the
        # patchright launch automatically. On residential IPs (the
        # operator's Mac), leave PROXY unset.
        proxy_cfg = bb.patchright_proxy_from_env()
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(
                    headless=False,
                    proxy=proxy_cfg,
                )
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

                # Warm up Akamai cookies + bot score: visit the
                # careers page, wait for its JS challenge to settle,
                # then mimic a few seconds of human-shaped scroll +
                # mouse movement. The behavioural signals matter on
                # datacentre IPs even with patchright's fingerprint
                # mask — without them we get a 429 cpr_chlge instead
                # of a 200.
                await page.goto(
                    f"{_BASE_URL}{_CAREERS_HOME}",
                    wait_until="networkidle",
                    timeout=60_000,
                )
                await page.wait_for_timeout(5_000)
                await page.evaluate("window.scrollBy(0, 500)")
                await page.wait_for_timeout(2_000)
                await page.evaluate("window.scrollBy(0, -300)")
                await page.wait_for_timeout(2_000)
                await page.mouse.move(700, 400)
                await page.wait_for_timeout(1_000)
                await page.mouse.move(900, 600)
                await page.wait_for_timeout(2_000)

                # Sanity check before the API call: if the warmup
                # produced an Access Denied page, fail fast with a
                # diagnostic the operator can act on (proxy
                # exhausted, patchright outdated vs new Akamai
                # rules, …).
                if "access denied" in (await page.content()).lower():
                    raise ScraperError(
                        "Tesla: warmup page is 'Access Denied'. "
                        "patchright did not pass Akamai — set PROXY "
                        "to a residential proxy or run from a "
                        "residential IP."
                    )

                # Fire the API call from inside the page context so
                # cookies + Sec-Fetch metadata match the bot-scored
                # session. ``page.goto`` to the JSON URL works
                # locally on residential IPs but tends to slip the
                # Sec-Fetch-Dest header through proxies, while
                # ``fetch()`` from page JS keeps it consistent.
                api = await page.evaluate(
                    """async () => {
                        const r = await fetch(
                            '/cua-api/apps/careers/state',
                            {credentials: 'include'}
                        );
                        return {status: r.status, body: await r.text()};
                    }"""
                )
                if api["status"] != 200:
                    raise ScraperError(
                        f"Tesla: /cua-api/state returned "
                        f"status={api['status']} after warmup."
                    )
                payload = self._parse_json(api["body"])
            finally:
                await browser.close()

        return list(self._parse_payload(payload))

    @staticmethod
    def _parse_json(body: str) -> dict[str, Any]:
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
