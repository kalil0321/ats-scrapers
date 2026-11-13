"""CATS career portals.

CATS is an applicant-tracking system used mostly by staffing and recruiting
firms. A customer has a careers portal at

    https://{tenant}.catsone.com/careers/

which redirects to ``/careers/{portalId}`` and lists every open job on one
page. The page is rendered by a React Router application and carries the
data it was rendered from:

    window.__reactRouterContext.streamController.enqueue("[{\\"_1\\":2},...]")

The argument is a string holding one flat JSON array in the turbo-stream
encoding React Router uses: an object is written ``{"_<index of the key>":
<index of the value>}``, an array is a list of indexes, a negative number
stands for a missing value, and a list that starts with a letter is a typed
value (``["D", 1792598173000]`` is a date in milliseconds). Decoded, it
gives the portal and its jobs:

    loaderData["portals/cats-careers/portal"]["portal"]
        {"portalId": "7701", "name": "emergiTEL Inc.", "active": true, ...}
    loaderData["portals/cats-careers/jobs"]["jobs"]
        [{"jobId": "16864772", "positionTitle": "Account Support Rep",
          "city": "Windsor", "state": "ON",
          "datePosted": "2026-10-16T07:34:42.000Z",
          "fields": {"jobType": "contract", "country": "Canada",
                     "salary": "$25.78/hour", "remoteType": null, ...}}]

The job page (``/careers/{portalId}/jobs/{jobId}``) carries the same way
the job with its description (``descriptionHtml``, whose text is kept) and
its country code.

Job numbers are the same on every portal of CATS, so ``ats_id`` is the job
number alone.

``company_slug`` is the tenant label (``emergitel``), the tenant and a
portal number (``emergitel/7701``) for a tenant's other portals, or a URL
of the portal.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

DOMAIN = "catsone.com"
# At least this share of the jobs listed must be read into a job.
MIN_COVERAGE = 0.9
DETAIL_CONCURRENCY = 4
MAX_DESCRIPTION_CHARS = 25_000

_HOST_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)\.catsone\.com$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PORTAL_RE = re.compile(r"^\d{1,12}$")
_PORTAL_PATH_RE = re.compile(r"^/careers/(\d{1,12})(?:[-/]|$)")
_JOB_ID_RE = re.compile(r"^\d{1,18}$")
_ENQUEUE = "streamController.enqueue("

_PORTAL_ROUTE = "portals/cats-careers/portal"
_JOBS_ROUTE = "portals/cats-careers/jobs"
_JOB_ROUTE = "portals/cats-careers/job"

# CATS's own values for the kind of hire. A direct hire can be full time or
# part time, so it maps to no employment type.
_CONTRACT_TYPES = frozenset({"contract", "contract-to-hire"})
_FULLY_REMOTE = "fully remote"


@ScraperRegistry.register(ATSType.CATSONE)
class CatsOneScraper(BaseScraper):
    """All jobs of one CATS careers portal."""

    ats = ATSType.CATSONE
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        company_name: str | None = None,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        try:
            self.tenant, self.portal = _parse_slug(company_slug)
        except ValueError as exc:
            raise ScraperError(str(exc)) from exc
        self.base_url = f"https://{self.tenant}.{DOMAIN}"
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            jobs = await self._list(fetch)
            if self.include_descriptions and jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(
                    *(self._enrich(fetch, sem, job) for job in jobs)
                )
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                await self._enrich(fetch, asyncio.Semaphore(1), job)
            return job.description

        return self._run_sync(run())

    async def _list(self, fetch: Fetcher) -> list[Job]:
        path = f"/careers/{self.portal}" if self.portal else "/careers/"
        page = await fetch.get_text(f"{self.base_url}{path}")
        try:
            data = _loader_data(page)
            portal = _portal(data)
            rows = _rows(data)
        except ValueError as exc:
            raise ScraperError(
                f"CATS ({self.company_slug}): {exc}"
            ) from exc
        if not portal.active:
            raise CompanyNotFoundError(
                f"CATS ({self.company_slug}): portal {portal.number} is not active"
            )
        jobs: dict[str, Job] = {}
        read = 0
        for row in rows:
            job = self._job(row, portal)
            if job is not None and job.ats_id:
                read += 1
                jobs.setdefault(job.ats_id, job)
        # A job listed twice is one job; it is the rows that are counted.
        if read < math.ceil(len(rows) * MIN_COVERAGE):
            raise ScraperError(
                f"CATS ({self.company_slug}) could read only {read} of "
                f"the {len(rows)} jobs listed"
            )
        return list(jobs.values())

    def _job(self, row: object, portal: _Portal) -> Job | None:
        if not isinstance(row, dict):
            return None
        number = _text(row.get("jobId"))
        title = _text(row.get("positionTitle"))
        if not number or not _JOB_ID_RE.match(number) or not title:
            return None
        fields = row.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        kind = _text(fields.get("jobType"))
        remote = _text(fields.get("remoteType"))
        raw = {
            key: value
            for key, value in (
                ("portal", portal.number),
                ("branch", _text(fields.get("branch"))),
                ("category", _text(fields.get("category"))),
                ("duration", _text(fields.get("duration"))),
                ("remote_type", remote),
                ("date_modified", _text(row.get("dateModified"))),
            )
            if value
        }
        return Job(
            url=f"{self.base_url}/careers/{portal.number}/jobs/{number}",
            title=title,
            company=self.company_name or portal.name or self.tenant,
            ats_type=ATSType.CATSONE,
            ats_id=number,
            location=_location(row, fields),
            is_remote=True if remote and remote.casefold() == _FULLY_REMOTE else None,
            salary_summary=_text(fields.get("salary")),
            employment_type=(
                "CONTRACT" if kind and kind.casefold() in _CONTRACT_TYPES else None
            ),
            commitment=kind,
            department=_text(fields.get("department")),
            posted_at=_date(row.get("datePosted")),
            fetched_at=datetime.now(UTC),
            raw=raw,
        )

    async def _enrich(
        self, fetch: Fetcher, sem: asyncio.Semaphore, job: Job
    ) -> None:
        # Best-effort: a job page that fails must not drop the job.
        async with sem:
            try:
                page = await fetch.get_text(str(job.url))
            except ScraperError:
                return
        try:
            route = _loader_data(page).get(_JOB_ROUTE)
        except ValueError:
            return
        detail = route.get("job") if isinstance(route, dict) else None
        if not isinstance(detail, dict) or _text(detail.get("jobId")) != job.ats_id:
            return
        markup = detail.get("descriptionHtml")
        if isinstance(markup, str) and not job.description:
            # The description is plain text in the dataset: the text of the
            # markup, one line per block.
            text = parse_html(markup).lines()[:MAX_DESCRIPTION_CHARS].strip()
            job.description = text or None
        code = _text(detail.get("countryCode"))
        if code and re.fullmatch(r"[A-Za-z]{2}", code) and job.country_iso is None:
            job.country_iso = code.upper()
        if job.posted_at is None:
            job.posted_at = _date(detail.get("datePosted"))


@dataclass(slots=True)
class _Portal:
    number: str
    name: str | None
    active: bool


def _loader_data(page: str) -> dict[str, Any]:
    """The data a portal page was rendered from, by route."""
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    start = page.find(_ENQUEUE)
    while start != -1:
        try:
            chunk, _ = decoder.raw_decode(page, start + len(_ENQUEUE))
        except ValueError:
            chunk = None
        if isinstance(chunk, str):
            chunks.append(chunk)
        start = page.find(_ENQUEUE, start + len(_ENQUEUE))
    if not chunks:
        raise ValueError("the page carries no portal data")
    # The first line is the page's data; later lines are values the page
    # receives afterwards.
    first_line = "".join(chunks).split("\n", 1)[0]
    try:
        flat = json.loads(first_line)
    except ValueError as exc:
        raise ValueError("the portal data is not valid JSON") from exc
    root = _decode(flat)
    data = root.get("loaderData") if isinstance(root, dict) else None
    if not isinstance(data, dict):
        raise ValueError("the portal data has no route data")
    return data


def _decode(flat: object) -> object:
    """Rebuild the value a turbo-stream array stands for.

    Every object and array of the result is created first and filled next,
    so values that refer to each other, or to themselves, are rebuilt
    without recursion.
    """
    if not isinstance(flat, list) or not flat:
        raise ValueError("the portal data is not a list of values")
    shells: dict[int, dict[str, Any] | list[Any]] = {}
    for index, value in enumerate(flat):
        if isinstance(value, dict):
            shells[index] = {}
        elif isinstance(value, list) and not (value and isinstance(value[0], str)):
            shells[index] = []

    def at(index: object) -> object:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("the portal data refers to a value by no index")
        if index < 0:
            # Null, undefined and the other values with no content.
            return None
        if index >= len(flat):
            raise ValueError("the portal data refers to a value it does not hold")
        if index in shells:
            return shells[index]
        value = flat[index]
        return _typed(value) if isinstance(value, list) else value

    for index, shell in shells.items():
        value = flat[index]
        if isinstance(shell, dict):
            for key, target in value.items():
                name = at(int(key[1:])) if re.fullmatch(r"_\d+", key) else None
                if not isinstance(name, str):
                    raise ValueError("the portal data has an object key that is no name")
                shell[name] = at(target)
        else:
            shell.extend(at(item) for item in value)
    return at(0)


def _typed(value: list[Any]) -> object:
    """A typed value: a date is read, the other kinds are not needed."""
    if len(value) == 2 and value[0] == "D":
        stamp = value[1]
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            try:
                return datetime.fromtimestamp(stamp / 1000, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None
    return None


def _portal(data: dict[str, Any]) -> _Portal:
    route = data.get(_PORTAL_ROUTE)
    portal = route.get("portal") if isinstance(route, dict) else None
    number = _text(portal.get("portalId")) if isinstance(portal, dict) else None
    if not isinstance(portal, dict) or not number or not _PORTAL_RE.match(number):
        raise ValueError("the page is not a careers portal")
    return _Portal(
        number=number,
        name=_text(portal.get("name")),
        # A portal is taken as closed only when it says so.
        active=portal.get("active") is not False,
    )


def _rows(data: dict[str, Any]) -> list[Any]:
    route = data.get(_JOBS_ROUTE)
    rows = route.get("jobs") if isinstance(route, dict) else None
    if not isinstance(rows, list):
        raise ValueError("the portal page carries no job list")
    return rows


def _location(row: dict[str, Any], fields: dict[str, Any]) -> str | None:
    city = _text(row.get("city"))
    state = _text(row.get("state"))
    country = _text(fields.get("country"))
    if city or state:
        return ", ".join(part for part in (city, state, country) if part)
    # No city and no state: the place as the portal words it, if any.
    place = _text(fields.get("location"))
    return ", ".join(part for part in (place, country) if part) or None


def _date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        cleaned = re.sub(r"\s+", " ", value).strip()
        return cleaned or None
    return None


def _parse_slug(value: str) -> tuple[str, str | None]:
    """``(tenant, portal number or None)`` from a slug or a portal URL."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(
            "CATS needs a tenant label ('emergitel'), a tenant and a portal "
            "number ('emergitel/7701') or a portal URL"
        )
    if "://" in raw:
        parsed = urlparse(raw)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError(f"CATS URL must be a plain https:// URL, got {value!r}")
        host = _HOST_RE.match((parsed.hostname or "").lower().rstrip("."))
        if not host:
            raise ValueError(
                f"CATS host must be '{{tenant}}.{DOMAIN}', got {value!r}"
            )
        portal = _PORTAL_PATH_RE.match(parsed.path)
        return host.group(1), portal.group(1) if portal else None
    label, _, portal = raw.lower().partition("/")
    host = _HOST_RE.match(label)
    tenant = host.group(1) if host else label
    if not _LABEL_RE.match(tenant):
        raise ValueError(
            f"CATS tenant must be one host label of {DOMAIN}, got {value!r}"
        )
    if portal and not _PORTAL_RE.match(portal):
        raise ValueError(f"CATS portal number must be digits, got {value!r}")
    return tenant, portal or None
