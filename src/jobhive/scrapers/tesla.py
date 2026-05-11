"""Tesla careers scraper — cloakbrowser-backed.

Tesla's public listings live at
``https://www.tesla.com/cua-api/apps/careers/state``, which returns
the entire job catalog as one JSON document. Direct ``httpx`` calls
are 403'd by Akamai bot management — TLS-impersonation libraries
(``httpcloak``, ``curl_cffi``) and even Browserbase Sessions get
"Access Denied" because Akamai pins the IP / TLS fingerprint /
JavaScript challenge stack together.

``cloakbrowser`` (stealth-patched Chromium) clears the bot manager
in our 2026-05-11 retesting. From a datacenter VPS the unproxied
request to ``cua-api`` gets rate-limited (429) even via cloakbrowser,
so we route the whole flow through the Evomi residential proxy when
``PROXY`` is set. The behavioural warm-up (scroll + mouse moves +
short waits) primes Akamai's risk-score before we touch the API.

Graceful degradation: when ``cloakbrowser`` isn't installed, the
scraper logs a warning and returns ``[]`` so the rest of the publish
pipeline keeps moving (per the optional-browser-fallback contract).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers import _cloakbrowser as cb
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

_BASE_URL = "https://www.tesla.com"
_CAREERS_HOME = "/careers/search/"
_STATE_ENDPOINT = "/cua-api/apps/careers/state"

# Page-load waits that let Akamai's risk-score settle before the
# ``cua-api`` call. Tuned to ~10 s total wall — long enough to look
# human, short enough to leave headroom for cron's 02:40 budget.
_INITIAL_SETTLE_S = 5
_POST_SCROLL_S = 2
_POST_MOUSE_S = 2


@ScraperRegistry.register(ATSType.TESLA)
class TeslaScraper(BaseScraper):
    """Tesla scraper. Single tenant — slug is ignored."""

    ats = ATSType.TESLA

    def fetch(self) -> list[Job]:
        if not cb.is_enabled():
            cb.warn_disabled("Tesla")
            return []
        return asyncio.run(self._fetch_via_cloakbrowser())

    async def _fetch_via_cloakbrowser(self) -> list[Job]:
        from cloakbrowser import launch_async

        proxy = cb.evomi_proxy_from_env()
        browser = await launch_async(
            headless=True, humanize=True, proxy=proxy,
        )
        try:
            page = await browser.new_page()

            # Warm up Akamai cookies + risk-score with a real-looking
            # visit to the careers page.
            await page.goto(
                f"{_BASE_URL}{_CAREERS_HOME}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await asyncio.sleep(_INITIAL_SETTLE_S)
            await page.mouse.wheel(0, 500)
            await asyncio.sleep(_POST_SCROLL_S)
            await page.mouse.wheel(0, -300)
            await asyncio.sleep(_POST_SCROLL_S)
            await page.mouse.move(400, 400, steps=20)
            await page.mouse.move(800, 600, steps=20)
            await asyncio.sleep(_POST_MOUSE_S)

            # Fetch the state endpoint from inside the page context
            # so we keep the warm-up cookies. ``fetch`` returns the
            # raw text — Tesla's endpoint is JSON, not the legacy
            # ``<pre>``-wrapped form.
            resp = await page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {credentials: 'include'});
                    return {status: r.status, body: await r.text()};
                }""",
                _STATE_ENDPOINT,
            )
        finally:
            await browser.close()

        if resp["status"] != 200:
            raise ScraperError(
                f"Tesla cua-api returned status {resp['status']} "
                f"(body preview: {resp['body'][:200]!r})"
            )
        try:
            payload = json.loads(resp["body"])
        except json.JSONDecodeError as exc:
            raise ScraperError(
                f"Tesla: response did not parse as JSON ({exc})."
            ) from exc

        return list(self._parse_payload(payload))

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
