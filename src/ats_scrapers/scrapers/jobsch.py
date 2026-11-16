"""jobs.ch — Switzerland's largest direct-posting job board (~50k active).

Companies pay to list on jobs.ch — postings are not syndicated from
LinkedIn / Indeed. Coverage spans all of Switzerland (DE-CH, FR-CH,
IT-CH, EN) across every sector (the API doesn't restrict to tech).
The May 2026 audit had Switzerland at 0.2% of the dataset; this is
roughly a 25× lift.

Public REST API at ``https://www.jobs.ch/api/v1/public/search`` — no
auth, no key. Pagination is ``?start=N&rows=20`` (rows hard-capped
at 20; >20 → 422). Each entry has ``company_name`` embedded so no
separate company-resolution fetch is needed. The detail-page URL
template is in ``_links.detail_*`` (German is the canonical default).

Two anti-bot quirks to handle:

  - Datacenter IP geo-fence — bare httpx from a Hetzner / AWS /
    DigitalOcean machine returns 403 with a 919-byte block page
    (verified 2026-05-09 from Hetzner). The scraper tries direct
    first and, on 403, falls back to a residential proxy pulled
    from the ``PROXY`` env var (Evomi 4-colon shape
    ``http://host:port:user:pass``, matching Tesla / Meta).
    Without ``PROXY`` set we raise a clear error rather than
    silently 0-scraping.

  - Deep-pagination cap — ``start>=2000`` always returns 422
    regardless of IP, so one query reads at most 2 000 of the ~37 000
    live postings. Facet filters do not help: many postings carry no
    region, category or industry. Every posting but a few dozen has an
    initial publication date, so the scraper splits the board into
    ``publication-date-from`` / ``publication-date-to`` windows (local
    time, ``Y-m-d H:i:s``, from inclusive, to exclusive) and halves any
    window that still matches more than 2 000 postings. The unfiltered
    newest 2 000 are read first, which also picks up recent postings
    without a date.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import Fetcher, proxy_url_from_env
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://www.jobs.ch/api/v1/public/search"
PER_PAGE = 20  # API hard-caps ``rows`` at 20 (>20 → 422).
MAX_CONCURRENCY = 4
DETAIL_CONCURRENCY = 4
# The API hard-caps deep pagination at start=2000 (==page 100). Any
# per-query fetch therefore tops out at 2 000 rows.
MAX_USABLE_OFFSET = 2000
# Default cap on pages per query — 100 × 20 rows = the API's per-query
# ceiling. Lower via ``max_pages`` for quick smoke runs.
DEFAULT_MAX_PAGES = 100

_TAG_RE = re.compile(r"<[^>]+>")
_META_TAG_RE = re.compile(r"<meta\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_DESCRIPTION_BLOCK_RE = re.compile(
    r'<(?:div|section|article)[^>]+class=["\'][^"\']*(?:job-description|vacancy-description|description|jobad)[^"\']*["\'][^>]*>(?P<body>.*?)</(?:div|section|article)>',
    re.IGNORECASE | re.DOTALL,
)

# Lower bound of the publication-date walk; live postings are far younger.
EARLIEST_PUBLICATION = datetime(2000, 1, 1)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# A walk over today's board takes well under a hundred windows; a search
# that ignores the date filter would split until one-second windows.
MAX_DATE_WINDOWS = 1_000


class _BlockedError(Exception):
    """Internal marker — the API returned 403, retry the whole fetch
    via the residential-proxy fallback. Not raised at the public
    boundary."""


@ScraperRegistry.register(ATSType.JOBSCH)
class JobsChScraper(BaseScraper):
    """jobs.ch (Switzerland) — direct-posting board.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages``: page cap per query (default 100, the API's own
      2,000-row window).
    - ``query_seeds``: extra keyword searches run after the date walk.
    """

    ats = ATSType.JOBSCH

    default_headers: ClassVar[dict[str, str]] = {"User-Agent": "Mozilla/5.0"}

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        query_seeds: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.max_pages = max_pages
        self._windows = 0
        self.query_seeds: tuple[str, ...] = tuple(query_seeds or ())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()
        proxy_url = self.proxy or proxy_url_from_env()

        async def run() -> str | None:
            client_kwargs: dict[str, Any] = {
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if proxy_url is not None:
                client_kwargs["proxy"] = proxy_url
            async with httpx.AsyncClient(**client_kwargs) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(client, sem, copy)
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        # Probe IP routing on the empty query: if the datacenter IP is
        # 403-blocked, the same probe-then-proxy escalation runs once,
        # and every subsequent seed reuses the resulting proxy_url.
        try:
            return await self._fetch_all(proxy_url=None)
        except _BlockedError:
            pass

        proxy_url = proxy_url_from_env()
        if proxy_url is None:
            raise ScraperError(
                "jobs.ch returned 403 (likely datacenter IP block) and "
                "no PROXY env var is set. Set PROXY=http://host:port:user:pass "
                "to a residential proxy (Evomi or similar) to enable the "
                "fallback path."
            )
        log.info(
            "jobs.ch: direct request 403'd — retrying via PROXY "
            "residential fallback."
        )
        return await self._fetch_all(proxy_url=proxy_url)

    async def _fetch_all(self, *, proxy_url: str | None) -> list[Job]:
        """Read the unfiltered search, then every publication-date window,
        then each keyword seed, deduping by ``job_id``.

        A 403 before the proxy switch escalates to the caller (proxy
        fallback). Once in proxy mode, a 403 drops only that page, window
        or seed.
        """
        seen: set[str] = set()
        all_jobs: list[Job] = []
        in_proxy_mode = proxy_url is not None

        def absorb(items: list[dict[str, Any]]) -> None:
            for it in items:
                job = self._parse(it)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                all_jobs.append(job)

        fetcher = self.make_fetcher(proxy=proxy_url)
        # ``proxy_url=None`` means the direct-IP probe — it must NOT
        # fall back to the env proxy (the Fetcher's default when proxy
        # is None), because the residential proxy is reserved for the
        # explicit 403 escalation path in :meth:`afetch`.
        fetcher.proxy = proxy_url if proxy_url is not None else self.proxy

        async with fetcher:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            first = await self._fetch_page(fetcher, sem, start=0)
            total = int(first.get("total_hits") or 0)
            await self._drain(fetcher, sem, {}, first, absorb, in_proxy_mode)
            log.info("jobs.ch: unfiltered search → %d of %d rows", len(all_jobs), total)

            if total > MAX_USABLE_OFFSET:
                latest = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=2)
                await self._check_date_filter(fetcher, sem, latest)
                self._windows = 0
                await self._walk_dates(
                    fetcher, sem, EARLIEST_PUBLICATION, latest.replace(microsecond=0),
                    absorb, in_proxy_mode,
                )
                log.info(
                    "jobs.ch: date windows → %d unique rows of %d", len(all_jobs), total,
                )

            for seed in self.query_seeds:
                params = {"query": seed}
                try:
                    first = await self._fetch_page(fetcher, sem, start=0, params=params)
                    await self._drain(fetcher, sem, params, first, absorb, in_proxy_mode)
                except _BlockedError:
                    if not in_proxy_mode:
                        raise
                    log.warning("jobs.ch: query=%s blocked even via PROXY; skipping it.", seed)
                    continue
                log.info("jobs.ch: query=%s → total %d unique rows", seed, len(all_jobs))

        if self.include_descriptions and all_jobs:
            await self._enrich_descriptions(all_jobs, proxy_url=proxy_url)
        return all_jobs

    async def _check_date_filter(
        self, fetcher: Fetcher, sem: asyncio.Semaphore, latest: datetime,
    ) -> None:
        """Raise unless a window before 2000 and one a year ahead are empty.

        A search that ignores either bound would match postings there.
        """
        for start in (datetime(1990, 1, 1), latest + timedelta(days=365)):
            params = {
                "publication-date-from": start.strftime(_DATE_FORMAT),
                "publication-date-to": (start + timedelta(days=1)).strftime(_DATE_FORMAT),
            }
            page = await self._fetch_page(fetcher, sem, start=0, params=params)
            if int(page.get("total_hits") or 0):
                raise ScraperError(
                    f"jobs.ch matched postings in the empty window {params}; "
                    "the search no longer filters by publication date"
                )

    async def _walk_dates(
        self,
        fetcher: Fetcher,
        sem: asyncio.Semaphore,
        start: datetime,
        end: datetime,
        absorb: Callable[[list[dict[str, Any]]], None],
        in_proxy_mode: bool,
    ) -> None:
        """Read every posting first published in ``[start, end)``."""
        self._windows += 1
        if self._windows > MAX_DATE_WINDOWS:
            raise ScraperError(
                f"jobs.ch: the publication-date walk passed {MAX_DATE_WINDOWS} "
                "windows; refusing to go on"
            )
        params = {
            "publication-date-from": start.strftime(_DATE_FORMAT),
            "publication-date-to": end.strftime(_DATE_FORMAT),
        }
        try:
            first = await self._fetch_page(fetcher, sem, start=0, params=params)
        except _BlockedError:
            if not in_proxy_mode:
                raise
            log.warning("jobs.ch: window %s blocked even via PROXY; skipping it.", params)
            return
        total = int(first.get("total_hits") or 0)
        if total > MAX_USABLE_OFFSET:
            middle = (start + (end - start) / 2).replace(microsecond=0)
            if start < middle < end:
                await asyncio.gather(
                    self._walk_dates(fetcher, sem, start, middle, absorb, in_proxy_mode),
                    self._walk_dates(fetcher, sem, middle, end, absorb, in_proxy_mode),
                )
                return
            log.warning(
                "jobs.ch: %d postings first published at %s; only %d are reachable",
                total, params["publication-date-from"], MAX_USABLE_OFFSET,
            )
        await self._drain(fetcher, sem, params, first, absorb, in_proxy_mode)

    async def _drain(
        self,
        fetcher: Fetcher,
        sem: asyncio.Semaphore,
        params: dict[str, str],
        first: dict[str, Any],
        absorb: Callable[[list[dict[str, Any]]], None],
        in_proxy_mode: bool,
    ) -> None:
        """Absorb ``first`` and every following page of the same query."""
        absorb(first.get("documents") or [])
        total = int(first.get("total_hits") or 0)
        if total <= PER_PAGE:
            return
        usable = min(total, MAX_USABLE_OFFSET)
        page_count = min((usable + PER_PAGE - 1) // PER_PAGE, self.max_pages)

        async def one(offset: int) -> None:
            try:
                payload = await self._fetch_page(
                    fetcher, sem, start=offset, params=params
                )
            except _BlockedError:
                # After the proxy switch a 403 is a per-IP dropout: lose
                # the page, keep the rest.
                if not in_proxy_mode:
                    raise
                return
            absorb(payload.get("documents") or [])

        await asyncio.gather(*(one(PER_PAGE * i) for i in range(1, page_count)))

    async def _enrich_descriptions(
        self, jobs: list[Job], *, proxy_url: str | None
    ) -> None:
        client_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if proxy_url is not None:
            client_kwargs["proxy"] = proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
            await asyncio.gather(*(
                self._enrich_description(client, detail_sem, job) for job in jobs
            ))

    async def _enrich_description(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with sem:
            try:
                response = await client.get(
                    str(job.url),
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        description = _extract_description(response.text)
        if description and not job.description:
            job.description = description[:25_000]

    async def _fetch_page(
        self,
        fetcher: Fetcher,
        sem: asyncio.Semaphore,
        *,
        start: int,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"start": start, "rows": PER_PAGE, **(params or {})}
        # The shared Fetcher owns retries/backoff for 429/5xx and
        # network errors. 403 (datacenter IP block → proxy escalation)
        # and 422 (past the deep-pagination cap → exhausted) are
        # provider quirks handled here.
        async with sem:
            response = await fetcher.request(
                "GET",
                API_URL,
                params=query,
                headers={"Accept": "application/json"},
                handled={403, 422},
            )
        if response.status_code == 403:
            # Datacenter IP block — escalate to ``afetch`` so it can
            # retry the whole fetch through the residential proxy.
            # Don't burn retries here.
            raise _BlockedError(
                f"jobs.ch returned 403 at start={start}"
            )
        if response.status_code == 422:
            if start == 0:
                # The first page of a search is never past the cap: the
                # API rejected the search itself.
                raise ScraperError(
                    f"jobs.ch rejected the search {params or {}}: {response.text[:200]}"
                )
            # Past the search-engine cap (rare; API caps deep
            # pagination differently per query). Treat as exhausted.
            return {"documents": [], "total_hits": 0}
        try:
            return response.json()
        except ValueError as exc:
            raise ScraperError(
                f"jobs.ch returned non-JSON at start={start}: {exc}"
            ) from exc

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("job_id") or "")
        title = (item.get("title") or "").strip()
        company = (item.get("company_name") or "").strip()
        if not ats_id or not title:
            return None

        url = _detail_url(item, ats_id)

        # ``place`` is the city name; ``regions`` is a numeric path
        # (cantons + sub-regions) we don't have a name table for. The
        # city is enough for downstream geo-search.
        place = (item.get("place") or "").strip() or None
        location = f"{place}, Switzerland" if place else "Switzerland"

        # employment_grades is a list like [100] (% time). When the
        # only value is below 100 the role is part-time; when 100 it's
        # full-time; mixed lists indicate flexibility.
        grades = item.get("employment_grades") or []
        is_full_time = grades == [100]
        employment_type = "FULL_TIME" if is_full_time else (
            "PART_TIME" if grades and all(g < 100 for g in grades) else None
        )

        posted_at = _parse_iso(
            item.get("publication_date") or item.get("initial_publication_date")
        )

        raw: dict[str, Any] = {}
        if grades:
            raw["employment_grades"] = grades
        languages = [
            entry.get("language") for entry in (item.get("language_skills") or [])
            if isinstance(entry, dict) and entry.get("language")
        ]
        if languages:
            raw["languages"] = languages
        if item.get("company_id"):
            raw["company_id"] = str(item["company_id"])
        if item.get("company_segmentation"):
            raw["company_segmentation"] = item["company_segmentation"]

        return Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.JOBSCH,
            ats_id=ats_id,
            location=location,
            employment_type=employment_type,
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _detail_url(item: dict[str, Any], job_id: str) -> str:
    """Prefer ``_links.detail_{lang}.href`` when present (jobs.ch ships
    a localized detail URL per row), else fall back to the documented
    canonical English URL pattern.
    """
    links = item.get("_links") or {}
    if isinstance(links, dict):
        for key in ("detail_en", "detail_de", "detail_fr", "detail_it"):
            entry = links.get(key)
            if isinstance(entry, dict):
                href = entry.get("href")
                if isinstance(href, str) and href:
                    return href
    return f"https://www.jobs.ch/en/vacancies/detail/{job_id}/"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_description(text: str) -> str | None:
    match = _DESCRIPTION_BLOCK_RE.search(text)
    if match:
        cleaned = _strip_html(match.group("body"))
        if cleaned:
            return cleaned
    meta = _extract_meta_description(text)
    return meta or None


def _extract_meta_description(text: str) -> str | None:
    for tag in _META_TAG_RE.finditer(text):
        attrs = {
            m.group("name").lower(): html.unescape(m.group("value"))
            for m in _ATTR_RE.finditer(tag.group("attrs"))
        }
        kind = (attrs.get("name") or attrs.get("property") or "").lower()
        if kind not in {"description", "og:description"}:
            continue
        cleaned = _strip_html(attrs.get("content") or "")
        if cleaned:
            return cleaned
    return None


def _strip_html(value: str) -> str:
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


