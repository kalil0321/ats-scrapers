"""jobs.ch: Switzerland's largest direct-posting job board (~38k open in Nov 2026).

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

Three quirks to handle:

  - Datacenter IP geo-fence: bare httpx from a datacenter address
    returns 403 with a 919-byte block page. The scraper tries direct
    first and, on 403, falls back to a residential proxy pulled
    from the ``PROXY`` env var (``http://host:port:user:pass``,
    matching Tesla / Meta).
    Without ``PROXY`` set we raise a clear error rather than
    silently 0-scraping.

  - Blocks partway through a run: through the proxy, jobs.ch starts
    answering 403 to every request partway through a run and keeps
    doing so for minutes. A 403 after the first answered request
    pauses every search request, reopens the connections, retries the
    request and paces the rest of the run, slower after each new
    block. A block that outlasts every pause fails the fetch: the
    newest windows hold most of the board, so skipping one would
    publish a fraction of it.

  - Deep-pagination cap: ``start>=2000`` always returns 422
    regardless of IP, so one query reads at most 2 000 of the ~38 000
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
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import Fetcher, FetchResponse, proxy_url_from_env
from ats_scrapers.models import ATSType, Job, WorkloadPercentages
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers._posting_detail import body_text, fill_missing, job_posting
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from ats_scrapers.models import EngagementType
    from ats_scrapers.scrapers._htmltree import Node

log = logging.getLogger(__name__)

API_URL = "https://www.jobs.ch/api/v1/public/search"
PER_PAGE = 20  # API hard-caps ``rows`` at 20 (>20 → 422).
MAX_CONCURRENCY = 4
DETAIL_CONCURRENCY = 4
# Public jobs.ch translation keys employment_type.1 through employment_type.6.
# ID 4 ("Supplementary income") does not specify a contractual relationship.
_ENGAGEMENT_TYPES: dict[str, EngagementType] = {
    "1": "temporary", "2": "independent_contractor", "3": "internship",
    "5": "permanent", "6": "apprenticeship",
}
# The API hard-caps deep pagination at start=2000 (==page 100). Any
# per-query fetch therefore tops out at 2 000 rows.
MAX_USABLE_OFFSET = 2000
# Default cap on pages per query — 100 × 20 rows = the API's per-query
# ceiling. Lower via ``max_pages`` for quick smoke runs.
DEFAULT_MAX_PAGES = 100

# Lower bound of the publication-date walk; live postings are far younger.
EARLIEST_PUBLICATION = datetime(2000, 1, 1)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# A walk over today's board takes well under a hundred windows; a search
# that ignores the date filter would split until one-second windows.
MAX_DATE_WINDOWS = 1_000

# Seconds every search request waits after a 403 that follows an answered
# request, one entry per refusal in a row. A block lifts once the
# address's request rate has dropped, which takes minutes.
BLOCK_PAUSES = (30.0, 60.0, 120.0, 300.0, 300.0)
# Search requests per second after the first block, the second, and any later.
PACED_RATES = (2.0, 1.0, 0.5)


class _BlockedError(Exception):
    """Internal marker — the API returned 403, retry the whole fetch
    via the residential-proxy fallback. Not raised at the public
    boundary."""


class _Throttle:
    """Paces the search requests of one fetch and holds them all during a block.

    Requests already in flight when a pause begins neither lengthen it nor
    end the block: only the ones sent after it do.
    """

    def __init__(self) -> None:
        self.answered = False
        self.interval = 0.0
        self._blocks = 0
        self._refusals = 0
        self._paused_at = float("-inf")
        self._resume_at = 0.0
        self._next_start = 0.0

    async def wait(self) -> float:
        """Wait for this request's turn and return its start time."""
        loop = asyncio.get_running_loop()
        while (delay := max(self._resume_at, self._next_start) - loop.time()) > 0:
            await asyncio.sleep(delay)
        now = loop.time()
        self._next_start = now + self.interval
        return now

    def passed(self, sent_at: float) -> None:
        self.answered = True
        if sent_at >= self._paused_at:
            self._refusals = 0

    def paused_since(self, sent_at: float) -> bool:
        return sent_at < self._paused_at

    @property
    def spent(self) -> bool:
        return self._refusals == len(BLOCK_PAUSES)

    def pause(self) -> float:
        """Hold every request for the next pause and return its length."""
        if not self._refusals:
            self._blocks += 1
            self.interval = 1 / PACED_RATES[min(self._blocks, len(PACED_RATES)) - 1]
        seconds = BLOCK_PAUSES[self._refusals]
        self._refusals += 1
        self._paused_at = asyncio.get_running_loop().time()
        self._resume_at = self._paused_at + seconds
        return seconds


class _Search:
    """The client, concurrency and throttle shared by the search requests of one fetch."""

    def __init__(self, fetcher: Fetcher, *, via_proxy: bool) -> None:
        self.fetcher = fetcher
        self.via_proxy = via_proxy
        self.throttle = _Throttle()
        self.reopen = False
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._reopening = asyncio.Lock()
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def send(self, query: dict[str, Any]) -> tuple[float, FetchResponse]:
        """Send one search request in its turn; return its start time and response."""
        async with self._sem:
            async with self._reopening:
                if self.reopen:
                    # Closing the client cuts requests still in flight, and the
                    # Fetcher would resend those at once, inside the pause.
                    await self._idle.wait()
                    await self.fetcher.aclose()
                    self.reopen = False
            sent_at = await self.throttle.wait()
            self._in_flight += 1
            self._idle.clear()
            try:
                response = await self.fetcher.request(
                    "GET",
                    API_URL,
                    params=query,
                    headers={"Accept": "application/json"},
                    handled={403, 422},
                )
            finally:
                self._in_flight -= 1
                if not self._in_flight:
                    self._idle.set()
        return sent_at, response


async def _gather(coros: list[Coroutine[Any, Any, None]]) -> None:
    """Run ``coros`` together; on the first failure cancel the rest and raise it."""
    try:
        async with asyncio.TaskGroup() as group:
            for coro in coros:
                group.create_task(coro)
    except BaseExceptionGroup as failed:
        raise failed.exceptions[0] from None


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
        self._run_sync(self._enrich_descriptions([job], proxy_url=self.proxy or proxy_url_from_env()))
        return job.description

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
                "to a residential proxy to enable the "
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

        Without the proxy, a 403 on the first request, or a block that
        outlasts every pause, escalates to the caller (proxy fallback).
        Through the proxy, a block that outlasts every pause fails the fetch.
        """
        seen: set[str] = set()
        all_jobs: list[Job] = []

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
            search = _Search(fetcher, via_proxy=proxy_url is not None)
            first = await self._fetch_page(search, start=0)
            total = int(first.get("total_hits") or 0)
            await self._drain(search, {}, first, absorb)
            log.info("jobs.ch: unfiltered search → %d of %d rows", len(all_jobs), total)

            if total > MAX_USABLE_OFFSET:
                latest = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=2)
                await self._check_date_filter(search, latest)
                self._windows = 0
                await self._walk_dates(
                    search, EARLIEST_PUBLICATION, latest.replace(microsecond=0), absorb,
                )
                log.info(
                    "jobs.ch: date windows → %d unique rows of %d", len(all_jobs), total,
                )

            for seed in self.query_seeds:
                params = {"query": seed}
                first = await self._fetch_page(search, start=0, params=params)
                await self._drain(search, params, first, absorb)
                log.info("jobs.ch: query=%s → total %d unique rows", seed, len(all_jobs))

        if self.include_descriptions and all_jobs:
            await self._enrich_descriptions(all_jobs, proxy_url=proxy_url)
        return all_jobs

    async def _check_date_filter(self, search: _Search, latest: datetime) -> None:
        """Raise unless a window before 2000 and one a year ahead are empty.

        A search that ignores either bound would match postings there.
        """
        for start in (datetime(1990, 1, 1), latest + timedelta(days=365)):
            params = {
                "publication-date-from": start.strftime(_DATE_FORMAT),
                "publication-date-to": (start + timedelta(days=1)).strftime(_DATE_FORMAT),
            }
            page = await self._fetch_page(search, start=0, params=params)
            if int(page.get("total_hits") or 0):
                raise ScraperError(
                    f"jobs.ch matched postings in the empty window {params}; "
                    "the search no longer filters by publication date"
                )

    async def _walk_dates(
        self,
        search: _Search,
        start: datetime,
        end: datetime,
        absorb: Callable[[list[dict[str, Any]]], None],
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
        first = await self._fetch_page(search, start=0, params=params)
        total = int(first.get("total_hits") or 0)
        if total > MAX_USABLE_OFFSET:
            middle = (start + (end - start) / 2).replace(microsecond=0)
            if start < middle < end:
                await _gather([
                    self._walk_dates(search, start, middle, absorb),
                    self._walk_dates(search, middle, end, absorb),
                ])
                return
            log.warning(
                "jobs.ch: %d postings first published at %s; only %d are reachable",
                total, params["publication-date-from"], MAX_USABLE_OFFSET,
            )
        await self._drain(search, params, first, absorb)

    async def _drain(
        self,
        search: _Search,
        params: dict[str, str],
        first: dict[str, Any],
        absorb: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        """Absorb ``first`` and every following page of the same query."""
        absorb(first.get("documents") or [])
        total = int(first.get("total_hits") or 0)
        if total <= PER_PAGE:
            return
        usable = min(total, MAX_USABLE_OFFSET)
        page_count = min((usable + PER_PAGE - 1) // PER_PAGE, self.max_pages)

        async def one(offset: int) -> None:
            payload = await self._fetch_page(search, start=offset, params=params)
            absorb(payload.get("documents") or [])

        await _gather([one(PER_PAGE * i) for i in range(1, page_count)])

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
            async def enrich(job: Job) -> None:
                await self._enrich_description(client, job)

            await fill_missing(jobs, enrich, DETAIL_CONCURRENCY)

    async def _enrich_description(self, client: httpx.AsyncClient, job: Job) -> None:
        if job.ats_id:
            url = f"{API_URL}/job/{quote(job.ats_id, safe='')}"
            try:
                response = await client.get(url, headers={"Accept": "application/json"})
                if response.status_code in {401, 403, 410, 429}:
                    return
                if response.status_code == 200:
                    item = response.json()
                    if isinstance(item, dict) and item.get("job_id") == job.ats_id:
                        _apply_employment_fields(job, item)
                        job.description = _api_description(item)
                        if job.description:
                            return
            except (httpx.HTTPError, ValueError):
                pass
        try:
            page = await client.get(str(job.url), headers={"Accept": "text/html"})
        except httpx.HTTPError:
            return
        if page.status_code == 200:
            tree = parse_html(page.text)
            if job.workload_percentages is None:
                job.workload_percentages = _page_workload(tree)
            job.description = _extract_description(page.text, tree)

    async def _fetch_page(
        self,
        search: _Search,
        *,
        start: int,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"start": start, "rows": PER_PAGE, **(params or {})}
        throttle = search.throttle
        # The shared Fetcher owns retries/backoff for 429/5xx and
        # network errors. 403 (address blocked) and 422 (past the
        # deep-pagination cap) are provider quirks handled here.
        while True:
            sent_at, response = await search.send(query)
            if response.status_code != 403:
                throttle.passed(sent_at)
                break
            if not search.via_proxy and not throttle.answered:
                # The address is refused outright (datacenter IPs).
                raise _BlockedError(f"jobs.ch returned 403 at start={start}")
            if throttle.paused_since(sent_at):
                continue
            if throttle.spent:
                message = (
                    f"jobs.ch returned 403 at start={start} {params or {}} "
                    f"after pausing {sum(BLOCK_PAUSES):.0f}s for the block to lift"
                )
                if search.via_proxy:
                    raise ScraperError(message)
                raise _BlockedError(message)
            pause = throttle.pause()
            log.warning(
                "jobs.ch: 403 at start=%d %s; pausing every search request for %.0fs",
                start, params or {}, pause,
            )
            # A rotating proxy hands new connections a new exit address.
            search.reopen = True
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

        posted_at = _parse_iso(
            item.get("publication_date") or item.get("initial_publication_date")
        )

        raw: dict[str, Any] = {}
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

        job = Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.JOBSCH,
            ats_id=ats_id,
            description=_api_description(item),
            location=location,
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )
        _apply_employment_fields(job, item)
        return job


def _apply_employment_fields(job: Job, item: dict[str, Any]) -> None:
    raw = dict(job.raw or {})
    if grades := item.get("employment_grades"):
        raw["employment_grades"] = grades
        if isinstance(grades, list) and all(
            type(n) in (int, float) and 0 <= n <= 100 for n in grades
        ):
            job.workload_percentages = WorkloadPercentages(values=list(dict.fromkeys(grades)))
            if all(n == 100 for n in grades):
                job.employment_type = "FULL_TIME"
            elif all(n < 100 for n in grades):
                job.employment_type = "PART_TIME"
            else:
                job.employment_type = None
    if isinstance(ids := item.get("employment_type_ids"), list) and ids:
        raw["employment_type_ids"] = ids
        choices = {_ENGAGEMENT_TYPES.get(str(value)) for value in ids}
        job.engagement_type = choices.pop() if len(choices) == 1 else None
    job.raw = raw or None


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


def _api_description(item: dict[str, Any]) -> str | None:
    # Search and the multi-ID endpoint return a preview. Only the vacancy
    # endpoint normally supplies the employer's original template sections.
    body = body_text(item.get("template_text"))
    if body:
        sections = [body_text(item.get("template_lead_text")), body,
                    body_text(item.get("template_contact_address"))]
        return "\n\n".join(dict.fromkeys(s for s in sections if s))[:25_000]

    if isinstance(template := item.get("template"), str) and template.strip():
        root = parse_html(template)
        content = root.find("body") or root
        if content.find("p") or content.find("li"):
            return content.lines()[:25_000] or None
    return None


def _page_workload(tree: Node) -> WorkloadPercentages | None:
    for node in tree.walk():
        if node.attrs.get("data-cy") != "info-workload":
            continue
        match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(?:%?\s*[–-]\s*(\d+(?:[.,]\d+)?)\s*)?%", node.text())
        if match:
            low = float(match[1].replace(",", "."))
            high = float(match[2].replace(",", ".")) if match[2] else low
            if 0 <= low <= high <= 100:
                if match[2] is None:
                    return WorkloadPercentages(values=[low])
                return WorkloadPercentages(minimum=low, maximum=high)
    return None


def _extract_description(page: str, tree: Node) -> str | None:
    # The JSON-LD can be automatically translated. The visible vacancy
    # section preserves the original and may also contain a separate summary.
    for node in tree.walk():
        if node.attrs.get("data-cy") == "vacancy-description":
            labels = {"About the job", "Über den Job", "À propos de l’emploi"}
            node.children = [
                child for child in node.children
                if isinstance(child, str) or (
                    child.attrs.get("data-cy") != "info-block"
                    and not (child.tag == "h4" and child.text() in labels)
                )
            ]
        elif not {"vacancy-description", "job-description"}.intersection(node.classes):
            continue
        if text := node.lines():
            return text[:25_000]
    return body_text(job_posting(page).get("description"))[:25_000] or None
