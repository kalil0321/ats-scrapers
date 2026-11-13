"""Cegid Talentsoft career sites.

Talentsoft (now Cegid HR) hosts a customer's career site at
``https://{site}.talent-soft.com``. Every site lists its offers at one
address, ten to a page, whatever language the site is in:

    GET https://{site}.talent-soft.com/offre-de-emploi/liste-toutes-offres.aspx
        ?mode=list&page={n}

(``mode=list`` asks for the list on the sites that open on a map.) A page
carries the number of offers of the whole list
(``<span id="..._Pagination_TotalOffers">114 offre(s)</span>``), a pager
whose active item is the page being shown, and one ``li.ts-offer-list-item``
per offer:

    <li class="ts-offer-list-item offerlist-item">
      <h3><a class="ts-offer-list-item__title-link"
             href="/offre-de-emploi/emploi-acheteur-f-h_15653.aspx">Acheteur F/H</a></h3>
      <span data-reference="2026-15653" ...></span>
      <ul class="ts-offer-list-item__description">
        <li>Réf. : 2026-15653</li><li>13/11/2026</li><li>CDI</li><li>Anglet</li>
      </ul>
    </li>

A page past the end answers the first page again, which the pager shows:
the walk stops when the page shown is not the page asked for. A site with no
offer answers the same frame with a "no result" block and no count.

The list stops at its hundredth page, so a site with more offers than a
hundred pages hold cannot be read in full this way. The pager names its last
page, which tells on the first page. Such a site is read through one of its
search filters instead. The first page lists them with the number of offers
of each value:

    <a href="/offre-de-emploi/liste-toutes-offres.aspx?changefacet=1&amp;facet_JobFamily=3226"
       >Affaires juridiques (322)</a>

and ``?changefacet=1&facet_JobFamily=3226&mode=list&page={n}`` is the list
of that value. Other sites show the same filters as the lists of a search
form, whose element id ends with the filter's name
(``..._EngineCriteriaCollection_Contract``, option ``CDI (1448)``); the
address is the same. A filter is used only when its values add up to the whole
list and each fits in a hundred pages; the offers of all its values are
then the whole list. The site keeps the chosen values in its session, where
they add up from one request to the next, so these requests are sent
without cookies. When no filter qualifies the site raises instead of
returning the first part of its offers.

The lines under the title are chosen by each customer and carry no label,
so they are kept as listed (``raw["listed"]``) and not read by position. In
particular the date among them is not taken as the posting date: on a site
that also prints its publication date, the two differed.

The offer page holds the offer in ``#contenu-ficheoffre``, which is the
description. Customers lay it out their own way, but the fields the product
defines keep their element ids, and three are read by id: the contract
(``fldjobdescription_contract``), the place
(``fldlocation_joblocation`` and
``fldlocation_location_geographicalareacollection``) and the pay
(``fldjobdescription_salaryrange``).

``company_slug`` is the site label (``edf-recrute``) or any URL of the
site. ``ats_id`` is ``{site}:{offer number}``.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._htmltree import Node, collapse, parse_html
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

DOMAIN = "talent-soft.com"
LIST_PATH = "/offre-de-emploi/liste-toutes-offres.aspx"
# 1,000 pages is 10,000 offers: past this the walk raises instead of going
# on.
MAX_PAGES = 1_000
# At least this share of the offers announced must be read into a job.
MIN_COVERAGE = 0.9
DETAIL_CONCURRENCY = 4
MAX_DESCRIPTION_CHARS = 25_000

_HOST_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)\.talent-soft\.com$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# An offer page of this site: one folder, then a name ending in the offer
# number.
_OFFER_PATH_RE = re.compile(r"^/[A-Za-z0-9-]+/[^/?#\s]*_(\d+)\.aspx$")
_PAGE_PARAM_RE = re.compile(r"[?&]page=(\d+)(?:&|$)")
# A value of a search filter, in a link that selects it.
_FACET_PARAM_RE = re.compile(r"[?&]facet_([A-Za-z0-9_]+)=(-?\d+)(?=&|$)")
# The search form names each of its lists after the filter it sets.
_CRITERIA_ID_MARK = "EngineCriteriaCollection_"
# The number of offers of a filter value, at the end of its label.
_FACET_COUNT_RE = re.compile(r"\(([\d\s\u00a0\u202f.,]+)\)\s*$")
# A filtered list may differ from the number its filter announced by this
# share (offers come and go during the walk), and by two offers at least.
_FACET_DRIFT = 0.05
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2}(?:$|[-_])")

_TOTAL_ID_SUFFIX = "pagination_totaloffers"
_NO_RESULT_ID_PART = "corps_noresult"
_SEARCH_PANEL_ID_PART = "corps_affichemoteur"
_OFFER_CONTENT_ID = "contenu-ficheoffre"
_CONTRACT_ID = "fldjobdescription_contract"
_SALARY_ID = "fldjobdescription_salaryrange"
_PLACE_ID = "fldlocation_joblocation"
_AREA_ID = "fldlocation_location_geographicalareacollection"


@ScraperRegistry.register(ATSType.TALENTSOFT)
class TalentsoftScraper(BaseScraper):
    """All offers of one Talentsoft career site."""

    ats = ATSType.TALENTSOFT
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
            self.site = _parse_slug(company_slug)
        except ValueError as exc:
            raise ScraperError(str(exc)) from exc
        self.base_url = f"https://{self.site}.{DOMAIN}"
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            jobs = await self._walk(fetch)
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

    async def _walk(self, fetch: Fetcher) -> list[Job]:
        try:
            walked = await self._walk_list(fetch, {})
        except _ListCutError as cut:
            walked = await self._walk_by_filter(fetch, cut.listing)
        # Offers that could not be read, and offers the list announces but
        # does not give, both count against the floor.
        expected = max(walked.total, len(walked.listed))
        if len(walked.rows) < math.ceil(expected * MIN_COVERAGE):
            raise ScraperError(
                f"Talentsoft ({self.company_slug}) could read only "
                f"{len(walked.rows)} of the {expected} offers listed"
            )
        return [
            self._job(row, walked.language) for row in walked.rows.values()
        ]

    async def _walk_by_filter(
        self, fetch: Fetcher, first: _Listing
    ) -> _Walked:
        """Read a list too long for its pager, one filter value at a time."""
        total = first.total or 0
        capacity = first.last_page * len(first.identities)
        choice = _choose_filter(first.facets, total=total, capacity=capacity)
        if choice is None:
            raise ScraperError(
                f"Talentsoft ({self.company_slug}) announces {total} "
                f"offers but its list stops at page {first.last_page} "
                f"({capacity} offers), and no search filter splits it into "
                f"lists that fit; refusing to return a partial list"
            )
        key, values = choice
        whole = _Walked(total=total, language=first.language)
        for value, announced in values:
            part = await self._walk_list(
                fetch,
                {"changefacet": "1", f"facet_{key}": value},
                announced=announced,
            )
            whole.listed.update(part.listed)
            for number, row in part.rows.items():
                whole.rows.setdefault(number, row)
            whole.language = whole.language or part.language
        return whole

    async def _walk_list(
        self,
        fetch: Fetcher,
        selection: dict[str, str],
        *,
        announced: int | None = None,
    ) -> _Walked:
        """Walk the whole list, or the list of one filter value."""
        url = f"{self.base_url}{LIST_PATH}"
        walked = _Walked()
        page = 1
        while True:
            if selection:
                # The site adds each filter value it is sent to the ones
                # its session already holds: no session, no leftovers.
                fetch.cookies.clear()
            listing = _parse_listing(
                await fetch.get_text(
                    url, params={**selection, "mode": "list", "page": page}
                )
            )
            if not listing.is_offer_list:
                raise ScraperError(
                    f"Talentsoft ({self.company_slug}) page {page} is not an "
                    f"offer list (page title {listing.title!r})"
                )
            # A page past the end answers the first page again; the pager
            # says which page this is.
            if listing.page is not None and listing.page != page:
                break
            walked.language = walked.language or listing.language
            if listing.total is not None:
                walked.total = max(walked.total, listing.total)
            if page == 1 and announced is not None:
                drift = max(2, math.ceil(announced * _FACET_DRIFT))
                if abs(walked.total - announced) > drift:
                    raise ScraperError(
                        f"Talentsoft ({self.company_slug}) lists "
                        f"{walked.total} offers for a filter value that "
                        f"announced {announced}"
                    )
            # The count and the pager of a page are built together: the
            # pages the pager names hold every offer announced unless the
            # list is cut.
            reachable = listing.last_page * len(listing.identities)
            if page == 1 and reachable < walked.total:
                if not selection:
                    raise _ListCutError(listing)
                raise ScraperError(
                    f"Talentsoft ({self.company_slug}) announces "
                    f"{walked.total} offers for one filter value but its "
                    f"list stops at page {listing.last_page} ({reachable} "
                    f"offers); refusing to return a partial list"
                )
            fresh = [
                key for key in listing.identities if key not in walked.listed
            ]
            walked.listed.update(fresh)
            for row in listing.rows:
                walked.rows.setdefault(row.number, row)
            # The list ends at the page the pager shows no page after, once
            # every offer announced has been listed. A page that lists
            # nothing it has not listed before ends it too. Offers are
            # compared as listed, not as read, so a page of new offers that
            # cannot be read does not pass for the end.
            if not fresh or (
                not listing.has_next and len(walked.listed) >= walked.total
            ):
                break
            if page >= MAX_PAGES:
                raise ScraperError(
                    f"Talentsoft ({self.company_slug}) still lists new offers "
                    f"at page {page} (page guard {MAX_PAGES}); refusing to "
                    f"return a partial list"
                )
            page += 1
        return walked

    def _job(self, row: _Row, language: str | None) -> Job:
        return Job(
            url=f"{self.base_url}{row.path}",
            title=row.title,
            company=self.company_name or self.site,
            ats_type=ATSType.TALENTSOFT,
            ats_id=f"{self.site}:{row.number}",
            requisition_id=row.reference,
            language=language,
            fetched_at=datetime.now(UTC),
            raw={"listed": list(row.listed)} if row.listed else None,
        )

    async def _enrich(
        self, fetch: Fetcher, sem: asyncio.Semaphore, job: Job
    ) -> None:
        # Best-effort: an offer page that fails must not drop the offer.
        async with sem:
            try:
                page = await fetch.get_text(str(job.url))
            except ScraperError:
                return
        offer = _parse_offer(page)
        if offer.description and not job.description:
            job.description = offer.description
        if offer.contract:
            job.commitment = offer.contract
        if offer.location:
            job.location = offer.location
        if offer.salary:
            job.salary_summary = offer.salary


@dataclass(slots=True)
class _Row:
    """One offer of the list."""

    number: str
    path: str
    title: str
    reference: str | None = None
    listed: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Listing:
    """What one page of the list holds."""

    is_offer_list: bool
    title: str
    language: str | None
    total: int | None
    # The page the pager shows as the current one.
    page: int | None
    has_next: bool
    # The last page the pager names.
    last_page: int
    # What tells each listed offer from the others, readable or not.
    identities: list[str]
    rows: list[_Row]
    # The search filters the page offers: for each filter, its values and
    # the number of offers each announces.
    facets: dict[str, list[tuple[str, int]]] = field(default_factory=dict)


@dataclass(slots=True)
class _Walked:
    """What walking a list gave."""

    total: int = 0
    language: str | None = None
    # Every offer listed, readable or not.
    listed: set[str] = field(default_factory=set)
    rows: dict[str, _Row] = field(default_factory=dict)


class _ListCutError(Exception):
    """The list announces more offers than its pager reaches."""

    def __init__(self, listing: _Listing) -> None:
        super().__init__("list cut")
        self.listing = listing


@dataclass(slots=True)
class _Offer:
    """What an offer page gives."""

    description: str | None = None
    contract: str | None = None
    location: str | None = None
    salary: str | None = None


def _parse_listing(page: str) -> _Listing:
    root = parse_html(page)
    title_node = root.find("title")
    html_node = root.find("html")
    lang = html_node.attrs.get("lang", "") if html_node else ""
    total: int | None = None
    is_offer_list = False
    current: int | None = None
    linked_pages: list[int] = []
    items: list[Node] = []
    facets: dict[str, list[tuple[str, int]]] = {}
    for node in root.walk():
        identifier = node.attrs.get("id", "").lower()
        classes = node.classes
        if identifier.endswith(_TOTAL_ID_SUFFIX):
            is_offer_list = True
            digits = re.sub(r"\D", "", node.text())
            if digits:
                total = int(digits)
        elif _NO_RESULT_ID_PART in identifier or _SEARCH_PANEL_ID_PART in identifier:
            is_offer_list = True
        found: list[tuple[str, str, int]] = []
        if node.tag == "a" and "changefacet=1" in node.attrs.get("href", ""):
            facet = _parse_facet_link(node)
            if facet is not None:
                found.append(facet)
        elif node.tag == "select" and _CRITERIA_ID_MARK in node.attrs.get(
            "id", ""
        ):
            found.extend(_parse_criteria_list(node))
        for key, value, count in found:
            values = facets.setdefault(key, [])
            if value not in {known for known, _ in values}:
                values.append((value, count))
        if node.tag == "li" and "ts-offer-list-item" in classes:
            items.append(node)
        elif node.tag == "li" and "ts-ol-pagination-list-item" in classes:
            if "ts-ol-pagination-list-item--active" in classes:
                digits = node.text()
                if digits.isdigit():
                    current = int(digits)
            for link in node.walk():
                match = _PAGE_PARAM_RE.search(link.attrs.get("href", ""))
                if link.tag == "a" and match:
                    linked_pages.append(int(match.group(1)))
    identities: list[str] = []
    rows: list[_Row] = []
    for item in items:
        row = _parse_row(item)
        if row is None:
            # No offer number to go by: the row's own text, marked so that
            # it cannot be taken for a number.
            identities.append(f"?{item.text()}")
        else:
            identities.append(row.number)
            rows.append(row)
    return _Listing(
        is_offer_list=is_offer_list,
        title=title_node.text() if title_node else "",
        language=lang[:2].lower() if _LANGUAGE_RE.match(lang) else None,
        total=total,
        page=current,
        has_next=any(number > (current or 1) for number in linked_pages),
        last_page=max([current or 1, *linked_pages]),
        identities=identities,
        rows=rows,
        facets=facets,
    )


def _parse_facet_link(link: Node) -> tuple[str, str, int] | None:
    """The filter, the value and the count of a link that selects a value."""
    selected = _FACET_PARAM_RE.findall(link.attrs.get("href", ""))
    # One value, added: a link that also drops a value, or adds two, is not
    # the list of one value.
    if len(selected) != 1 or selected[0][1].startswith("-"):
        return None
    count = _FACET_COUNT_RE.search(link.text())
    if count is None:
        return None
    digits = re.sub(r"\D", "", count.group(1))
    if not digits:
        return None
    return selected[0][0], selected[0][1], int(digits)


def _parse_criteria_list(select: Node) -> list[tuple[str, str, int]]:
    """The filter and the values of one list of the search form."""
    key = select.attrs["id"].split(_CRITERIA_ID_MARK, 1)[1]
    if not re.fullmatch(r"[A-Za-z0-9_]+", key):
        return []
    found: list[tuple[str, str, int]] = []
    for option in select.walk():
        value = option.attrs.get("value", "")
        # "0" is the "choose a value" line.
        if option.tag != "option" or not value.isdigit() or value == "0":
            continue
        count = _FACET_COUNT_RE.search(option.text())
        digits = re.sub(r"\D", "", count.group(1)) if count else ""
        if digits:
            found.append((key, value, int(digits)))
    return found


def _choose_filter(
    facets: dict[str, list[tuple[str, int]]],
    *,
    total: int,
    capacity: int,
) -> tuple[str, list[tuple[str, int]]] | None:
    """The filter whose values split the list into lists that fit.

    Its values must add up to the whole list, so that no offer is left
    out, and each must fit in the pages the pager reaches. Among several,
    the one with the fewest values costs the fewest requests.
    """
    fitting = [
        (key, values)
        for key, values in facets.items()
        if sum(count for _, count in values) == total
        and all(0 < count <= capacity for _, count in values)
    ]
    if not fitting:
        return None
    return min(fitting, key=lambda item: (len(item[1]), item[0]))


def _parse_row(item: Node) -> _Row | None:
    """One ``li.ts-offer-list-item``, or ``None`` when it cannot be read."""
    link = None
    match = None
    for node in item.walk():
        if node.tag == "a" and "ts-offer-list-item__title-link" in node.classes:
            match = _OFFER_PATH_RE.match(node.attrs.get("href", ""))
            if match:
                link = node
                break
    if link is None or match is None:
        return None
    title = link.text()
    if not title:
        return None
    reference = None
    listed: list[str] = []
    for node in item.walk():
        if reference is None and node.attrs.get("data-reference"):
            reference = collapse(node.attrs["data-reference"]) or None
        if node.tag == "ul" and any(
            name.startswith("ts-offer-list-item__description") for name in node.classes
        ):
            for child in node.children:
                if isinstance(child, Node) and child.tag == "li":
                    text = child.text()
                    if text:
                        listed.append(text)
    return _Row(
        number=match.group(1),
        path=link.attrs["href"],
        title=title,
        reference=reference,
        listed=listed,
    )


def _parse_offer(page: str) -> _Offer:
    offer = _Offer()
    place = None
    area = None
    for node in parse_html(page).walk():
        identifier = node.attrs.get("id", "").lower()
        if not identifier:
            continue
        if identifier == _OFFER_CONTENT_ID:
            text = node.lines()[:MAX_DESCRIPTION_CHARS].strip()
            offer.description = offer.description or text or None
        elif identifier == _CONTRACT_ID:
            offer.contract = offer.contract or node.text() or None
        elif identifier == _SALARY_ID:
            offer.salary = offer.salary or node.text() or None
        elif identifier == _PLACE_ID:
            place = place or node.text() or None
        elif identifier == _AREA_ID:
            area = area or node.text() or None
    # The place first, then the area it is in, each as the site gives it.
    offer.location = ", ".join(part for part in (place, area) if part) or None
    return offer


def _parse_slug(value: str) -> str:
    """The site label from ``site`` or a URL of the career site."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(
            "Talentsoft needs a site label ('edf-recrute') or a career-site "
            "URL ('https://edf-recrute.talent-soft.com')"
        )
    if "://" in raw:
        parsed = urlparse(raw)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError(
                f"Talentsoft URL must be a plain https:// URL, got {value!r}"
            )
        match = _HOST_RE.match((parsed.hostname or "").lower().rstrip("."))
        if not match:
            raise ValueError(
                f"Talentsoft host must be '{{site}}.{DOMAIN}', got {value!r}"
            )
        return match.group(1)
    label = raw.lower()
    host = _HOST_RE.match(label)
    if host:
        return host.group(1)
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"Talentsoft site must be one host label of {DOMAIN}, got {value!r}"
        )
    return label
