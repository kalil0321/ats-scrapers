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
import html
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
# Per-job detail endpoint — note the path differs from state
# (no ``apps/`` segment). Yields ``jobDescription``,
# ``jobResponsibilities``, ``jobRequirements``,
# ``jobCompensationAndBenefits``, ``department``, ``timeType``.
# Pattern lifted from the legacy scraper at ``legacy/tesla/main.py``.
# Passed into the in-page fetch as a JS template-arg (see
# ``_fetch_details``) so this constant is the single source of truth
# and the path can't drift between Python and JS.
_JOB_DETAIL_ENDPOINT = "/cua-api/careers/job/{job_id}"

# Page-load waits that let Akamai's risk-score settle before the
# ``cua-api`` call. Tuned to ~10 s total wall — long enough to look
# human, short enough to leave headroom for cron's 02:40 budget.
_INITIAL_SETTLE_S = 5
_POST_SCROLL_S = 2
_POST_MOUSE_S = 2

# Description-fetch knobs. We fan out N detail requests via
# Promise.all inside the warmed-up page (so they share Akamai
# cookies + risk-score), then sleep between batches so we don't
# trip the per-IP rate limiter.
_DETAIL_CONCURRENCY = 10
_DETAIL_BATCH_DELAY_S = 0.3


@ScraperRegistry.register(ATSType.TESLA)
class TeslaScraper(BaseScraper):
    """Tesla scraper. Single tenant — slug is ignored."""

    ats = ATSType.TESLA

    def fetch(self) -> list[Job]:
        if not cb.is_enabled():
            cb.warn_disabled("Tesla")
            return []
        return asyncio.run(self._fetch_via_cloakbrowser())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        if not job.ats_id or not cb.is_enabled():
            return None

        async def run() -> str | None:
            from cloakbrowser import launch_async

            proxy = cb.evomi_proxy_from_env()
            browser = await launch_async(
                headless=True, humanize=True, proxy=proxy,
            )
            try:
                page = await browser.new_page()
                await page.goto(
                    f"{_BASE_URL}{_CAREERS_HOME}",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                await asyncio.sleep(_INITIAL_SETTLE_S)
                details = await self._fetch_details(page, [job.ats_id])
            finally:
                await browser.close()
            detail = details.get(job.ats_id)
            return _format_description(detail) if detail else None

        return asyncio.run(run())

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

            jobs = list(self._parse_payload(payload))

            # Per-job descriptions — fetched in-session from inside
            # the warmed-up page so we keep cookies + risk-score. The
            # state-endpoint listings are description-less, so without
            # this step every Tesla row ships with an empty
            # ``description`` (violates the
            # always-include-descriptions invariant).
            if self.include_descriptions:
                ids = [j.ats_id for j in jobs]
                details = await self._fetch_details(page, ids)
                for j in jobs:
                    d = details.get(j.ats_id)
                    if d:
                        j.description = _format_description(d) or None
                        if not j.department:
                            j.department = d.get("department") or None
        finally:
            await browser.close()

        return jobs

    async def _fetch_details(
        self,
        page: Any,
        job_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Fetch ``/cua-api/careers/job/{id}`` for every id, batched.

        Uses Promise.all inside the page so the cookies set during
        warm-up are reused. Errors per job are swallowed and the id
        is simply absent from the returned dict — the caller treats
        a missing entry as "no description available" so a partial
        Akamai trip doesn't drop the whole listings payload we
        already paid for.
        """
        if not job_ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(job_ids), _DETAIL_CONCURRENCY):
            batch = job_ids[i : i + _DETAIL_CONCURRENCY]
            try:
                results = await page.evaluate(
                    """async ({ids, pathTpl}) => {
                        return await Promise.all(ids.map(async (id) => {
                            try {
                                const r = await fetch(
                                    pathTpl.replace('{job_id}', id),
                                    {credentials: 'include',
                                     headers: {'Accept': 'application/json'}},
                                );
                                if (r.status !== 200) {
                                    return {id, status: r.status, data: null};
                                }
                                return {id, status: 200, data: await r.json()};
                            } catch (e) {
                                return {id, status: -1, error: String(e)};
                            }
                        }));
                    }""",
                    {"ids": batch, "pathTpl": _JOB_DETAIL_ENDPOINT},
                )
            except Exception as exc:
                # Whole-batch failure (e.g. page crashed). Log and
                # keep going — partial coverage beats zero.
                log.warning(
                    "Tesla: detail batch %d failed: %s", i, exc,
                )
                continue
            for item in results or []:
                if item.get("status") == 200 and item.get("data"):
                    out[str(item["id"])] = item["data"]
            # Sleep only between batches, not after the last one — saves
            # a needless ~300 ms per scraper run on the tail batch.
            if i + _DETAIL_CONCURRENCY < len(job_ids):
                await asyncio.sleep(_DETAIL_BATCH_DELAY_S)
        log.info(
            "Tesla: fetched %d/%d job descriptions",
            len(out), len(job_ids),
        )
        return out

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


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RUN_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _html_to_text(raw: str) -> str:
    """Strip HTML tags + unescape entities + normalise whitespace.

    Tesla's detail endpoint ships ``jobResponsibilities`` /
    ``jobRequirements`` with embedded ``<li>``, ``<ul>``, ``<p>``
    markup; ``Job.description`` is documented as plain text and
    several downstream consumers assume that. A simple
    regex-strip is sufficient here — these payloads are
    well-formed snippets, not arbitrary HTML.

    Order matters: unescape entities *first*, then strip tags. If
    we stripped tags before unescaping, an encoded tag like
    ``&lt;script&gt;...`` would survive the strip pass and then
    unescape back into a literal ``<script>`` in the output —
    leaking real HTML into ``Job.description``. Doing the unescape
    first means any decoded tags get caught by the strip pass.
    """
    # 1. Unescape entities so any encoded tags surface as real
    #    angle-brackets that the strip pass below will then remove.
    text = html.unescape(raw)
    # 2. Replace block-level closing/self-closing tags with newlines
    #    before stripping so list items stay on separate lines.
    text = re.sub(
        r"<\s*(?:br\s*/?|/li|/p|/div|/h[1-6]|/tr|/ul|/ol)\s*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    # 3. Strip any remaining tags.
    text = _TAG_RE.sub("", text)
    # 4. Collapse runs of horizontal whitespace; squash 3+ newlines.
    text = _WS_RUN_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _format_description(detail: dict[str, Any]) -> str:
    """Concatenate the four description-bearing fields from the
    Tesla per-job detail payload into a single string.

    Mirrors the legacy formatter at ``legacy/tesla/main.py`` so the
    description column reads with section headers (Description /
    Responsibilities / Requirements / Compensation & Benefits) — Tesla
    surfaces those as distinct fields and the structure is worth
    preserving for downstream consumers. Each section value is run
    through ``_html_to_text`` first because Tesla mixes HTML markup
    (``<li>``, ``<ul>``, ``<p>``) into these fields and
    ``Job.description`` must be plain text.
    """
    sections = (
        ("Description", detail.get("jobDescription")),
        ("Responsibilities", detail.get("jobResponsibilities")),
        ("Requirements", detail.get("jobRequirements")),
        ("Compensation & Benefits",
         detail.get("jobCompensationAndBenefits")),
    )
    parts = []
    for label, value in sections:
        if not isinstance(value, str):
            continue
        cleaned = _html_to_text(value)
        if cleaned:
            parts.append(f"{label}:\n{cleaned}")
    return "\n\n".join(parts)
