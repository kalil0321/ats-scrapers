"""Ashby scraper.

Ashby exposes a public JSON board at:
    https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

The compensation field, when present, is rich (range + currency + interval).

An Ashby employer can switch three public surfaces on and off separately:

* the API above;
* the hosted pages, ``https://jobs.ashbyhq.com/{slug}`` and one page per job;
* the embed view of those same pages (``?embed=js``), which Ashby's embed
  script loads into the employer's own careers site and which answers
  whenever the board exists.

So the scraper reads the API and, when the API says the board does not exist,
the embed view, which carries the same listing in its ``window.__appData``
object (``jobs.ashbyhq.com/whatnot`` listed 137 openings on 14 Nov 2026 while
the API answered 404). A board that is gone answers 404 on both.

It also checks, with one HEAD request, that the hosted pages are open. When
they are not (Cursor, Supercell, PostHog: the jobs show on the employer's own
site), the ``jobUrl`` the API returns leads to a "not found" page; the jobs
then link to the embed view of each posting, which opens that posting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, CompensationComponent, Job, normalize_compensation_period
from ats_scrapers.scrapers._posting_fields import finite_number
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
BOARD_TEMPLATE = "https://jobs.ashbyhq.com/{slug}"
# The view Ashby's embed script loads: it answers for every board that exists,
# hosted pages open or not.
EMBED_QUERY = "?embed=js"
APP_DATA_MARKER = "window.__appData"
# Posting pages read at once on the hosted-board path (one request per opening).
HOSTED_DETAIL_CONCURRENCY = 4
# With descriptions off, postings tried in a row before a listing whose
# posting pages all answer 404 is taken for unopenable.
HOSTED_OPENABLE_PROBES = 3

_INTERVAL_MAP = {
    "HOURLY": "HOUR",
    "DAILY": "DAY",
    "WEEKLY": "WEEK",
    "MONTHLY": "MONTH",
    "ANNUALLY": "YEAR",
    "YEARLY": "YEAR",
    # Ashby's newer format uses "1 YEAR", "1 HOUR", etc.
    "1 YEAR": "YEAR",
    "1 MONTH": "MONTH",
    "1 WEEK": "WEEK",
    "1 DAY": "DAY",
    "1 HOUR": "HOUR",
}

_EMPLOYMENT_TYPE_MAP = {
    "FULLTIME": "FULL_TIME",
    "FULL_TIME": "FULL_TIME",
    "PARTTIME": "PART_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "INTERNSHIP": "INTERN",
    "INTERN": "INTERN",
    "TEMPORARY": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.ASHBY)
class AshbyScraper(BaseScraper):
    ats = ATSType.ASHBY

    async def afetch(self) -> list[Job]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        board_url = BOARD_TEMPLATE.format(slug=quote(self.company_slug, safe="%"))
        async with self.make_fetcher() as fetch:
            organization: dict[str, Any] | None = None
            try:
                payload = await fetch.get_json(url)
            except CompanyNotFoundError:
                # The API is off for this board, or the board is gone. The
                # embed view tells which: it answers 404 for a board that no
                # longer exists.
                data = _app_data(await fetch.get_text(board_url + EMBED_QUERY), board_url)
                organization = data.get("organization")
                items = await self._hosted_board_items(fetch, board_url, data.get("jobBoard"))
            else:
                items = payload.get("jobs", [])
            if items and not await _hosted_pages_open(fetch, board_url):
                if organization is None:
                    organization = await _organization(fetch, board_url)
                careers_page = (organization or {}).get("customJobsPageUrl")
                for item in items:
                    _link_to_embed_view(item, careers_page)
        return [self._parse_job(item) for item in items]

    def get_description(self, job: Job) -> str | None:
        """Description of one job, read from its posting page when the job
        came from a hosted board fetched with descriptions off."""
        if job.description or (job.raw or {}).get("source") != "hosted_board":
            return job.description
        job_url = _with_embed_query(str(job.url))

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                detail = await _posting_detail(fetch, job_url)
            description = (detail or {}).get("descriptionHtml")
            return description if isinstance(description, str) and description else None

        return self._run_sync(run())

    async def _hosted_board_items(
        self, fetch: Fetcher, board_url: str, board: object
    ) -> list[dict[str, Any]]:
        """Openings of the board's embed view, shaped like the API's ``jobs`` entries."""
        if not isinstance(board, dict):
            raise CompanyNotFoundError(
                f"Ashby ({self.company_slug}) has no public job board"
            )
        postings = board.get("jobPostings")
        # An empty list is a board without openings. Anything else we cannot
        # read is an error: a changed page must not look like an empty board.
        if not isinstance(postings, list) or not all(
            isinstance(posting, dict) and posting.get("id") and posting.get("title")
            for posting in postings
        ):
            raise ScraperError(
                f"Ashby ({self.company_slug}) hosted board has an unreadable listing"
            )
        teams = {
            team["id"]: team
            for team in board.get("teams") or []
            if isinstance(team, dict) and team.get("id")
        }
        if not self.include_descriptions:
            # The listing alone, plus one posting page that answers: a
            # board can list postings whose pages answer nowhere (Personio,
            # 14 Nov 2026), and a job nobody can open is not published.
            postings = await _openable(fetch, board_url, postings)
            return [
                _hosted_item(posting, None, _posting_url(board_url, posting), teams)
                for posting in postings
            ]
        gate = asyncio.Semaphore(HOSTED_DETAIL_CONCURRENCY)

        async def item_for(posting: dict[str, Any]) -> dict[str, Any] | None:
            job_url = _posting_url(board_url, posting)
            async with gate:
                try:
                    detail = await _posting_detail(fetch, job_url + EMBED_QUERY)
                except CompanyNotFoundError:
                    return None  # closed between the listing and this request
                except ScraperError as exc:
                    # The listing says the opening exists. One page we cannot
                    # read costs its description, not the employer's jobs.
                    logger.warning("Ashby posting page %s unreadable: %s", job_url, exc)
                    detail = None
            return _hosted_item(posting, detail, job_url, teams)

        items = await _gather_cancel_on_error(*(item_for(posting) for posting in postings))
        return [item for item in items if item is not None]

    def _parse_job(self, item: dict[str, Any]) -> Job:
        comp = item.get("compensation")
        if not isinstance(comp, dict):
            comp = {}
        summary = (
            comp.get("compensationTierSummary")
            or comp.get("scrapeableCompensationSalarySummary")
        )
        salary_min, salary_max, currency, period = _parse_comp(comp)

        emp_type = (item.get("employmentType") or "").upper()
        employment_type = _EMPLOYMENT_TYPE_MAP.get(emp_type)

        # ``isRemote`` is only set when truly remote; ``workplaceType``
        # ("Remote" / "On-site" / "Hybrid") fills the gap. Hybrid stays
        # None — neither flag captures it cleanly.
        is_remote = item.get("isRemote") if isinstance(item.get("isRemote"), bool) else None
        if is_remote is None:
            wp = item.get("workplaceType")
            if isinstance(wp, str):
                wp_norm = wp.strip().lower().replace("-", "").replace(" ", "")
                if wp_norm == "remote":
                    is_remote = True
                elif wp_norm in ("onsite", "inperson", "office"):
                    is_remote = False

        # Description — prefer ``descriptionHtml`` over ``descriptionPlain``.
        # The HTML form retains paragraph breaks, bullet lists, and headings
        # (the plain text concatenates them into a single block), so a
        # markdownify step can turn it into clean markdown. Plain stays as a
        # last-ditch fallback.
        description = (
            item.get("descriptionHtml")
            or item.get("descriptionPlain")
            or None
        )

        secondary_locations = item.get("secondaryLocations") or []

        raw: dict[str, Any] = {}
        if item.get("_hosted_board"):
            raw["source"] = "hosted_board"
        if item.get("_hosted_pages_closed"):
            raw["hosted_pages"] = "closed"
            if item.get("_careers_page"):
                raw["careers_page"] = item["_careers_page"]
        if item.get("department"):
            raw["department"] = item["department"]
        if item.get("team"):
            raw["team"] = item["team"]
        if secondary_locations:
            raw["secondary_locations"] = [
                loc.get("location") for loc in secondary_locations
                if isinstance(loc, dict) and loc.get("location")
            ]
        if item.get("address"):
            raw["address"] = item["address"]
        if item.get("workplaceType"):
            raw["workplace_type"] = item["workplaceType"]
        if comp:
            # Keep the full compensation tier structure for downstream consumers
            # who want to surface bonus/equity/commission separately.
            raw["compensation_tiers"] = comp.get("compensationTiers")

        return Job(
            url=item.get("jobUrl") or item.get("applyUrl"),
            title=item["title"],
            company=self.company_slug,
            ats_type=ATSType.ASHBY,
            ats_id=item["id"],
            location=item.get("location"),
            is_remote=is_remote,
            description=description,
            employment_type=employment_type,
            department=item.get("department") if isinstance(item.get("department"), str) else None,
            team=item.get("team") if isinstance(item.get("team"), str) else None,
            apply_url=item.get("applyUrl") if item.get("applyUrl") != item.get("jobUrl") else None,
            salary_currency=currency,
            salary_period=period,
            salary_summary=summary,
            compensation_components=_compensation_components(comp, raw),
            salary_min=salary_min,
            salary_max=salary_max,
            posted_at=_parse_iso(item.get("publishedAt")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _app_data(page: str, url: str) -> dict[str, Any]:
    """The ``window.__appData`` object Ashby's hosted pages embed."""
    marker = page.find(APP_DATA_MARKER)
    start = page.find("{", marker) if marker >= 0 else -1
    if start < 0:
        raise ScraperError(f"Ashby page {url} carries no {APP_DATA_MARKER} object")
    try:
        data, _ = json.JSONDecoder().raw_decode(page, start)
    except ValueError as exc:
        raise ScraperError(f"Ashby page {url}: unreadable {APP_DATA_MARKER} object") from exc
    if not isinstance(data, dict):
        raise ScraperError(f"Ashby page {url}: {APP_DATA_MARKER} is not an object")
    return data


async def _gather_cancel_on_error(*coros: Any) -> list[Any]:
    """``asyncio.gather`` that cancels and drains its siblings when one task
    raises, so no request is still running when the fetcher closes."""
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _posting_answers(fetch: Fetcher, job_url: str) -> bool:
    """Does this posting page answer? Only a 404 says no.

    A timeout, a block or a server error leaves the question open and counts
    as an answer: an availability check must not cost an employer the jobs
    its listing just returned.
    """
    try:
        await fetch.get_text(job_url)
    except CompanyNotFoundError:
        return False
    except ScraperError as exc:
        logger.warning("Ashby posting page %s could not be checked: %s", job_url, exc)
    return True


async def _openable(
    fetch: Fetcher, board_url: str, postings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The postings of a listing, from the first one whose page answers.

    A posting that just closed answers 404 and is dropped, like on the
    descriptions-on path. When ``HOSTED_OPENABLE_PROBES`` postings in a row
    answer 404, none of the listing can be opened and nothing is returned.
    """
    for position, posting in enumerate(postings[:HOSTED_OPENABLE_PROBES]):
        if await _posting_answers(fetch, _posting_url(board_url, posting) + EMBED_QUERY):
            return postings[position:]
    return []


def _with_embed_query(url: str) -> str:
    return url if "?" in url else url + EMBED_QUERY


async def _hosted_pages_open(fetch: Fetcher, board_url: str) -> bool:
    """Do the board's hosted pages answer? One HEAD request.

    Only a 404 says they are closed. Any other failure leaves the question
    open, and the links as the API gave them: a link check must not cost an
    employer its jobs.
    """
    try:
        response = await fetch.request("HEAD", board_url, handled={404})
    except ScraperError as exc:
        logger.warning("Ashby hosted page %s could not be checked: %s", board_url, exc)
        return True
    return response.status_code != 404


async def _organization(fetch: Fetcher, board_url: str) -> dict[str, Any] | None:
    """The organisation block of the board's embed view, or ``None`` when unreadable."""
    try:
        organization = _app_data(
            await fetch.get_text(board_url + EMBED_QUERY), board_url
        ).get("organization")
    except ScraperError as exc:
        logger.warning("Ashby embed view of %s unreadable: %s", board_url, exc)
        return None
    return organization if isinstance(organization, dict) else None


def _link_to_embed_view(item: dict[str, Any], careers_page: object) -> None:
    """Point a job at the embed view of its posting.

    The hosted pages of this board are closed, so ``jobUrl`` and ``applyUrl``
    as given lead to "not found". The embed view of the same two pages opens
    the posting and its application form.
    """
    item["_hosted_pages_closed"] = True
    if isinstance(careers_page, str) and careers_page.startswith(("http://", "https://")):
        item["_careers_page"] = careers_page
    for key in ("jobUrl", "applyUrl"):
        if isinstance(item.get(key), str) and item[key]:
            item[key] = _with_embed_query(item[key])


async def _posting_detail(fetch: Fetcher, job_url: str) -> dict[str, Any]:
    """The ``posting`` object of one hosted posting page."""
    detail = _app_data(await fetch.get_text(job_url), job_url).get("posting")
    if not isinstance(detail, dict):
        raise ScraperError(f"Ashby posting page {job_url} carries no posting")
    return detail


def _posting_url(board_url: str, posting: dict[str, Any]) -> str:
    return f"{board_url}/{quote(str(posting['id']), safe='')}"


def _team_names(team_id: object, teams: dict[str, dict[str, Any]]) -> list[str]:
    """Names from the top department down to the posting's own team."""
    names: list[str] = []
    seen: set[object] = set()
    while team_id in teams and team_id not in seen:
        seen.add(team_id)
        team = teams[team_id]
        if isinstance(team.get("name"), str):
            names.append(team["name"])
        team_id = team.get("parentTeamId")
    return names[::-1]


def _hosted_item(
    posting: dict[str, Any],
    detail: dict[str, Any] | None,
    job_url: str,
    teams: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """One hosted-board opening in the shape of an API ``jobs`` entry.

    The listing gives id, title, place, type and team; the posting page
    (``detail``, absent when descriptions are off) adds the description and
    the pay summary. Neither carries a publication date.
    """
    detail = detail or {}
    team_names = [name for name in detail.get("teamNames") or [] if isinstance(name, str)]
    if not team_names:
        team_names = _team_names(posting.get("teamId"), teams)
    secondary = detail.get("secondaryLocationNames")
    if not isinstance(secondary, list):
        secondary = [
            place.get("locationName")
            for place in posting.get("secondaryLocations") or []
            if isinstance(place, dict)
        ]
    item: dict[str, Any] = {
        "_hosted_board": True,
        "id": posting["id"],
        "title": detail.get("title") or posting["title"],
        "location": detail.get("locationName") or posting.get("locationName"),
        "secondaryLocations": [{"location": name} for name in secondary if name],
        "employmentType": detail.get("employmentType") or posting.get("employmentType"),
        "workplaceType": detail.get("workplaceType") or posting.get("workplaceType"),
        "department": detail.get("departmentName") or (team_names[0] if team_names else None),
        "team": team_names[-1] if team_names else None,
        "descriptionHtml": detail.get("descriptionHtml"),
        "jobUrl": job_url,
        "applyUrl": f"{job_url}/application",
    }
    summary = detail.get("compensationTierSummary") or posting.get("compensationTierSummary")
    scrapeable = detail.get("scrapeableCompensationSalarySummary")
    if summary or scrapeable:
        item["compensation"] = {
            "compensationTierSummary": summary,
            "scrapeableCompensationSalarySummary": scrapeable,
        }
    return item


_COMPONENT_TYPES = {
    "Salary": ("base_salary", "money"),
    "Bonus": ("bonus", None),
    "Commission": ("commission", None),
    "EquityCashValue": ("equity", "money"),
    "EquityPercentage": ("equity", "percent"),
    "EquityShares": ("equity", "shares"),
    "Allowance": ("allowance", None),
}
_TOTAL_PAY = re.compile(r"\b(?:on[ -]target earnings|OTE|total (?:compensation|pay))\b", re.I)
_PERCENT = re.compile(r"\d\s*(?:%|per\s*cent\b)|\bpercentage\b", re.I)


def _components_in_tier(
    records: object, tier: str | None, tier_summary: object,
) -> list[CompensationComponent]:
    components: list[CompensationComponent] = []
    if not isinstance(records, list):
        return components
    for record in records:
        if not isinstance(record, dict):
            continue
        label = record.get("compensationType")
        if not isinstance(label, str) or not label.strip():
            continue
        summary = record.get("summary")
        summary = summary if isinstance(summary, str) else None
        # Only a single-component tier summary can describe this row alone.
        if not summary and len(records) == 1 and isinstance(tier_summary, str):
            summary = tier_summary
        currency = record.get("currencyCode")
        currency = currency if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) else None
        kind, unit = _COMPONENT_TYPES.get(label, ("other", None))
        if kind in {"bonus", "commission", "allowance"}:
            if _PERCENT.search(summary or ""):
                unit = "percent"
            elif currency or re.search(r"[$€£¥₹]\s*\d", summary or ""):
                unit = "money"
        if label == "Salary" and _TOTAL_PAY.search(summary or ""):
            kind = "other"
        components.append(CompensationComponent.model_validate({
            "kind": kind, "source_label": label, "unit": unit,
            "currency": currency if unit == "money" else None,
            "period": normalize_compensation_period(record.get("interval")),
            "minimum": finite_number(record.get("minValue")),
            "maximum": finite_number(record.get("maxValue")),
            "tier": tier, "summary": summary,
        }))
    return components


def _compensation_components(comp: dict[str, Any], raw: dict[str, Any]) -> list[CompensationComponent] | None:
    components = []
    tiers = comp.get("compensationTiers")
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            scope = " — ".join(
                v for v in (tier.get("title"), tier.get("additionalInformation"))
                if isinstance(v, str) and v.strip()
            )
            if not scope and len(tiers) > 1 and isinstance(tier.get("id"), str):
                scope = tier["id"]
            components.extend(_components_in_tier(
                tier.get("components"), scope or None, tier.get("tierSummary"),
            ))
    # summaryComponents aggregates the tiers, so never append it to their rows.
    if components:
        return components
    records = comp.get("summaryComponents")
    components = _components_in_tier(records, None, comp.get("compensationTierSummary"))
    if components:
        raw["compensation_summary_components"] = records
    return components or None


def _parse_comp(
    comp: dict[str, Any] | None,
) -> tuple[float | None, float | None, str | None, str | None]:
    """Pull structured min/max/currency/period from a compensation tier.

    Ashby returns multiple component types (Salary, Bonus, Commission, Equity*).
    Salary is the field we want to surface as min/max. Field names live at the
    component level (`minValue`, `maxValue`, `currencyCode`) — not nested in
    `compensationValue` as some older docs suggest.
    """
    if not comp or not isinstance(comp.get("compensationTiers"), list):
        return None, None, None, None
    for tier in comp["compensationTiers"]:
        if not isinstance(tier, dict) or not isinstance(tier.get("components"), list):
            continue
        for component in tier["components"]:
            if not isinstance(component, dict) or component.get("compensationType") != "Salary":
                continue
            interval = _INTERVAL_MAP.get(component.get("interval", ""), "YEAR")
            return (
                component.get("minValue"),
                component.get("maxValue"),
                component.get("currencyCode"),
                interval,
            )
    return None, None, None, None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
