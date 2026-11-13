"""PeopleAdmin applicant portals.

PeopleAdmin is an applicant-tracking system used mostly by colleges and
universities. A customer has a portal at ``https://{tenant}.peopleadmin.com``
whose search page lists what is open to applications, thirty rows to a page:

    GET https://{tenant}.peopleadmin.com/postings/search?page={n}

Every page carries the number of rows of the whole list, in the heading of
the results (``View Results (416 )``), a pagination bar whose "next" control
is a link on every page but the last, and one ``div.job-item`` per row:

    <div class='job-item job-item-posting' data-posting-title="Librarian">
      <h3><a href="/postings/3968">Librarian</a></h3>
      <div class="tbody-cell">F205</div>
      <div class="tbody-cell">Library</div>
      <span class='job-description'>The first lines of the posting...</span>
    </div>

A page past the end answers the same frame, with the same count and no row.

Two kinds of rows share the list and its count: postings
(``/postings/{id}``) and applicant pools (``/pools/{id}``, shown with
"(Pool)" after the title), which are standing recruitments such as adjunct
or tutor pools that take applications like a posting does. Both are read;
``raw["kind"]`` tells them apart and ``include_pools=False`` leaves the
pools out.

The portal's own Atom feed (``/postings/all_jobs.atom``) is not used: on
the portals compared it was a subset of the search list, and stale on some.

The columns of a row, and the fields of a posting, are chosen by each
institution, so nothing is read by position. A cell is paired with the
column header above it only when the row has exactly as many cells as the
list has headers, and a field is used only when its label is one of the few
spelled out below (``Department``, ``Posting Number``, ``Open Date``, ...).

The posting page (``/postings/{id}``) is a table of ``th``/``td`` rows, one
per field. The description is every field that has a value, in the page's
order, as ``label``/``value`` blocks: there is no single description field
to pick, and which fields are the body of the posting differs from one
institution to the next.

``company_slug`` is the tenant label (``utah``) or any URL of the portal.
``ats_id`` is ``{tenant}:{id}`` for a posting and ``{tenant}:pool-{id}``
for a pool, the two being numbered separately.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ats_scrapers.fetch import Fetcher

DOMAIN = "peopleadmin.com"
# 500 pages is 15,000 rows: past this the walk raises instead of going on.
MAX_PAGES = 500
# At least this share of the rows announced must be read into a job.
MIN_COVERAGE = 0.9
DETAIL_CONCURRENCY = 4
MAX_DESCRIPTION_CHARS = 25_000

_HOST_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)\.peopleadmin\.com$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_ROW_LINK_RE = re.compile(r"^/(postings|pools)/(\d+)$")
_TOTAL_RE = re.compile(r"\(\s*(\d[\d,]*)\s*\)")
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")

# Field labels, as institutions spell them, compared case-insensitively and
# without a trailing colon. Where several are listed in a tuple, the first
# one that has a value wins.
_REQUISITION_LABELS = (
    "posting number",
    "requisition number",
    "job number",
    "requisition",
    "pool number",
)
_DEPARTMENT_LABELS = ("department", "department name")
_POSTED_LABELS = (
    "open date",
    "job open date",
    "posting date",
    "posted date",
    "date posted",
    "posting open date",
    "posting start date",
    "application open date",
)
_CLOSE_LABELS = (
    "close date",
    "job close date",
    "closing date",
    "application deadline",
    "application close date",
    "job application deadline",
)
# The most specific place first.
_LOCATION_LABELS = (
    "city",
    "location city",
    "position location (city)",
    "working location (city, state)",
    "work location",
    "job location",
    "position location",
    "location",
    "location(s)",
    "primary location",
    "hiring location",
    "hiring location code",
    "campus location",
    "city/campus",
    "campus/center",
    "campus",
    "on which campus will this work be done?",
    "site",
)
_SALARY_LABELS = (
    "salary",
    "salary range",
    "pay rate",
    "pay range",
    "anticipated pay range",
    "hiring salary range",
    "hiring salary",
    "starting salary",
    "salary information",
    "salary/salary range",
    "salary/pay range",
    "compensation",
)
# What institutions write in a place field when there is no place to give.
_NO_PLACE = frozenset({"n/a", "na", "none", "other", "tbd", "varies", "various"})

_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "address", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
)
_HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})
# The end tag of anything but a table part closes nothing outside the table
# cell it is in.
_CELL_TAGS = frozenset({"td", "th"})
_TABLE_TAGS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th"})
# Elements nested deeper than this are kept as leaves of the last level, so
# that markup pasted into a field cannot make the tree arbitrarily deep.
_MAX_DEPTH = 200


@ScraperRegistry.register(ATSType.PEOPLEADMIN)
class PeopleAdminScraper(BaseScraper):
    """All postings and applicant pools of one PeopleAdmin portal."""

    ats = ATSType.PEOPLEADMIN
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        company_name: str | None = None,
        include_pools: bool = True,
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
            self.tenant = _parse_slug(company_slug)
        except ValueError as exc:
            raise ScraperError(str(exc)) from exc
        self.base_url = f"https://{self.tenant}.{DOMAIN}"
        self.include_pools = include_pools
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            rows = await self._walk(fetch)
            jobs = [
                self._job(row)
                for row in rows
                if self.include_pools or row.kind != "pool"
            ]
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

    async def _walk(self, fetch: Fetcher) -> list[_Row]:
        url = f"{self.base_url}/postings/search"
        rows: dict[str, _Row] = {}
        # Every row listed so far, readable or not.
        listed: set[str] = set()
        total = 0
        page = 1
        while True:
            listing = _parse_listing(
                await fetch.get_text(url, params={"page": page})
            )
            if not listing.is_search_page:
                raise ScraperError(
                    f"PeopleAdmin ({self.company_slug}) page {page} is not a "
                    f"search page (page title {listing.title!r})"
                )
            if listing.total is not None:
                total = max(total, listing.total)
            fresh = [key for key in listing.identities if key not in listed]
            listed.update(fresh)
            for row in listing.rows:
                rows.setdefault(row.key, row)
            # The list ends at the page that has no "next" link, once every
            # row announced has been listed. A page that lists nothing it
            # has not listed before (no row past the end, or the same rows
            # again) ends it too. Rows are compared as listed, not as read,
            # so a page of new rows that cannot be read does not pass for
            # the end.
            if not fresh or (not listing.has_next and len(listed) >= total):
                break
            if page >= MAX_PAGES:
                raise ScraperError(
                    f"PeopleAdmin ({self.company_slug}) still lists new rows "
                    f"at page {page} (page guard {MAX_PAGES}); refusing to "
                    f"return a partial list"
                )
            page += 1
        # Rows that could not be read, and rows the list announces but does
        # not give, both count against the floor.
        expected = max(total, len(listed))
        if len(rows) < math.ceil(expected * MIN_COVERAGE):
            raise ScraperError(
                f"PeopleAdmin ({self.company_slug}) could read only "
                f"{len(rows)} of the {expected} rows listed"
            )
        return list(rows.values())

    def _job(self, row: _Row) -> Job:
        raw: dict[str, object] = {"kind": row.kind}
        if row.columns:
            raw["columns"] = dict(row.columns)
        if row.summary:
            raw["summary"] = row.summary
        job = Job(
            url=f"{self.base_url}/{_PATHS[row.kind]}/{row.number}",
            title=row.title,
            company=self.company_name or self.tenant,
            ats_type=ATSType.PEOPLEADMIN,
            ats_id=f"{self.tenant}:{row.key}",
            fetched_at=datetime.now(UTC),
            raw=raw,
        )
        _apply_fields(job, list(row.columns.items()))
        return job

    async def _enrich(
        self, fetch: Fetcher, sem: asyncio.Semaphore, job: Job
    ) -> None:
        # Best-effort: a posting page that fails must not drop the posting.
        async with sem:
            try:
                page = await fetch.get_text(str(job.url))
            except ScraperError:
                return
        fields = _posting_fields(page)
        if not fields:
            return
        columns = job.raw.get("columns") if job.raw else None
        listed = list(columns.items()) if isinstance(columns, dict) else []
        # The list's columns first: for a label both give, the list's value.
        _apply_fields(job, [*listed, *fields])
        description = _description(fields)
        if description and not job.description:
            job.description = description


_PATHS = {"posting": "postings", "pool": "pools"}


@dataclass(slots=True)
class _Row:
    """One row of the search list."""

    kind: str
    number: str
    title: str
    columns: dict[str, str] = field(default_factory=dict)
    summary: str | None = None

    @property
    def key(self) -> str:
        return self.number if self.kind == "posting" else f"pool-{self.number}"


@dataclass(slots=True)
class _Listing:
    """What one search page holds."""

    is_search_page: bool
    title: str
    total: int | None
    has_next: bool
    # What tells each listed row from the others, readable or not.
    identities: list[str]
    rows: list[_Row]


class _Node:
    """An element of a parsed page."""

    __slots__ = ("attrs", "children", "tag")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def walk(self) -> Iterator[_Node]:
        """Every element below this one, in document order."""
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.walk()

    def find(self, tag: str) -> _Node | None:
        return next((node for node in self.walk() if node.tag == tag), None)

    def _pieces(self, out: list[str]) -> None:
        for child in self.children:
            if isinstance(child, str):
                out.append(child)
            elif child.tag not in _HIDDEN_TAGS:
                block = child.tag in _BLOCK_TAGS
                if block:
                    out.append("\n")
                child._pieces(out)
                if block:
                    out.append("\n")

    def lines(self) -> str:
        """The text, one line per block, without blank lines."""
        pieces: list[str] = []
        self._pieces(pieces)
        lines = (_collapse(line) for line in "".join(pieces).split("\n"))
        return "\n".join(line for line in lines if line)

    def text(self) -> str:
        """The text on one line."""
        return _collapse(self.lines())


class _TreeBuilder(HTMLParser):
    """Builds the element tree of a page with the standard-library parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("", {})
        self._open: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(node)
        if tag not in _VOID_TAGS and len(self._open) < _MAX_DEPTH:
            self._open.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, 0, -1):
            current = self._open[index].tag
            if current == tag:
                del self._open[index:]
                return
            if current in _CELL_TAGS and tag not in _TABLE_TAGS:
                # A stray end tag inside a cell (rich text pasted into a
                # field) must not close the cell or what is around it.
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)


def _parse_html(page: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(page)
    builder.close()
    return builder.root


def _parse_listing(page: str) -> _Listing:
    root = _parse_html(page)
    title_node = root.find("title")
    headers: list[str] = []
    items: list[_Node] = []
    total: int | None = None
    is_search_page = False
    has_next = False
    for node in root.walk():
        identifier = node.attrs.get("id", "")
        classes = node.classes
        if identifier == "search_results":
            is_search_page = True
        elif identifier == "search-results":
            match = _TOTAL_RE.search(node.text())
            if match:
                total = int(match.group(1).replace(",", ""))
        if identifier.startswith("job_list_header") and not headers:
            headers = _column_headers(node)
        elif node.tag == "a" and "next_page" in classes and node.attrs.get("href"):
            has_next = True
        elif node.tag == "div" and "job-item" in classes:
            items.append(node)
    identities: list[str] = []
    rows: list[_Row] = []
    for item in items:
        row = _parse_row(item, headers)
        if row is None:
            # No number to go by: the row's own text, marked so that it
            # cannot be taken for a number.
            identities.append(f"?{item.text()}")
        else:
            identities.append(row.key)
            rows.append(row)
    return _Listing(
        is_search_page=is_search_page,
        title=title_node.text() if title_node else "",
        total=total,
        has_next=has_next,
        identities=identities,
        rows=rows,
    )


def _column_headers(container: _Node) -> list[str]:
    """The headers of the columns beside the title, from the header row.

    Each one is the link that sorts the list by that column. The title
    column has such a link on some portals; it is left out, the title being
    read from the row's heading.
    """
    headers: list[str] = []

    def visit(node: _Node) -> None:
        for child in node.children:
            if not isinstance(child, _Node) or "job-title" in child.classes:
                continue
            if child.tag == "a" and "sort=" in child.attrs.get("href", ""):
                headers.append(child.text())
            else:
                visit(child)

    visit(container)
    return headers


def _parse_row(item: _Node, headers: list[str]) -> _Row | None:
    """One ``div.job-item`` of the list, or ``None`` when it cannot be read."""
    heading = item.find("h3")
    if heading is None:
        return None
    link = None
    match = None
    for node in heading.walk():
        if node.tag == "a":
            match = _ROW_LINK_RE.match(node.attrs.get("href", ""))
            if match:
                link = node
                break
    if link is None or match is None:
        return None
    kind = "posting" if match.group(1) == "postings" else "pool"
    # The heading rather than the link: a pool's "(Pool)" is beside the link.
    title = heading.text() or link.text()
    if not title:
        return None
    cells: list[str] = []
    summary = None
    for node in item.walk():
        classes = node.classes
        if "job-description" in classes:
            summary = summary or node.text() or None
        elif node.tag == "div" and (
            "tbody-cell" in classes
            or any(name.startswith("col-md-push-") for name in classes)
        ):
            cells.append(node.text())
    columns: dict[str, str] = {}
    # No position is guessed: the cells are paired with the headers only
    # when there is exactly one cell per header.
    if headers and len(cells) == len(headers):
        for header, value in zip(headers, cells, strict=True):
            if header and value and header not in columns:
                columns[header] = value
    return _Row(
        kind=kind,
        number=match.group(2),
        title=title,
        columns=columns,
        summary=summary,
    )


def _posting_fields(page: str) -> list[tuple[str, str]]:
    """The ``(label, value)`` rows of a posting page, in the page's order."""
    fields: list[tuple[str, str]] = []

    def visit(node: _Node, in_cell: bool) -> None:
        for child in node.children:
            if not isinstance(child, _Node):
                continue
            if child.tag == "tr" and not in_cell:
                label = next(
                    (c for c in child.children if isinstance(c, _Node) and c.tag == "th"),
                    None,
                )
                value = next(
                    (c for c in child.children if isinstance(c, _Node) and c.tag == "td"),
                    None,
                )
                if label is not None and value is not None:
                    name = label.text()
                    content = value.lines()
                    if name and content:
                        fields.append((name, content))
                    # A table pasted into a field is part of that field's
                    # text, not a set of fields of its own.
                    continue
            visit(child, in_cell or child.tag in ("td", "th"))

    visit(_parse_html(page), False)
    return fields


def _description(fields: list[tuple[str, str]]) -> str | None:
    blocks = [f"{label}\n{value}" for label, value in fields]
    description = "\n\n".join(blocks)[:MAX_DESCRIPTION_CHARS].strip()
    return description or None


def _apply_fields(job: Job, fields: list[tuple[str, str]]) -> None:
    """Set what the labelled values give.

    For each field of the job the labels are tried in their order of
    preference; among values under one label, the first given.
    """
    by_label: dict[str, str] = {}
    for label, value in fields:
        text = _collapse(str(value))
        if text:
            by_label.setdefault(_label_key(label), text)

    def first(labels: tuple[str, ...]) -> str | None:
        return next((by_label[name] for name in labels if name in by_label), None)

    requisition = first(_REQUISITION_LABELS)
    if requisition:
        job.requisition_id = requisition
    department = first(_DEPARTMENT_LABELS)
    if department:
        job.department = department
    salary = first(_SALARY_LABELS)
    if salary:
        job.salary_summary = salary
    location = next(
        (
            by_label[name]
            for name in _LOCATION_LABELS
            if name in by_label and by_label[name].casefold() not in _NO_PLACE
        ),
        None,
    )
    if location:
        job.location = location
    # A posting on the list was opened by now; an opening date further
    # ahead than a day (the portal's own time zone) is not one.
    latest = datetime.now(UTC) + timedelta(days=1)
    posted_at = next(
        (
            parsed
            for parsed in (_parse_date(by_label.get(name)) for name in _POSTED_LABELS)
            if parsed is not None and parsed <= latest
        ),
        None,
    )
    if posted_at is not None:
        job.posted_at = posted_at
    # The closing day is kept as the institution states it: the hour and
    # the time zone it closes in are not given, so no instant is derived.
    close_date = next(
        (
            by_label[name]
            for name in _CLOSE_LABELS
            if _parse_date(by_label.get(name)) is not None
        ),
        None,
    )
    if close_date:
        job.raw = {**(job.raw or {}), "close_date": close_date}


def _label_key(label: str) -> str:
    return _collapse(label).rstrip(":").strip().casefold()


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_date(value: str | None) -> datetime | None:
    """A ``MM/DD/YYYY`` date, the only form the portals print."""
    if not value:
        return None
    match = _DATE_RE.match(value.strip())
    if not match:
        return None
    month, day, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _parse_slug(value: str) -> str:
    """The tenant label from ``tenant`` or a URL of the portal."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(
            "PeopleAdmin needs a tenant label ('utah') or a portal URL "
            "('https://utah.peopleadmin.com')"
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
                f"PeopleAdmin URL must be a plain https:// URL, got {value!r}"
            )
        match = _HOST_RE.match((parsed.hostname or "").lower().rstrip("."))
        if not match:
            raise ValueError(
                f"PeopleAdmin host must be '{{tenant}}.{DOMAIN}', got {value!r}"
            )
        return match.group(1)
    label = raw.lower()
    host = _HOST_RE.match(label)
    if host:
        return host.group(1)
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"PeopleAdmin tenant must be one host label of {DOMAIN}, got {value!r}"
        )
    return label
