"""Tests for the PeopleAdmin applicant-portal scraper."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import httpx
import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import PeopleAdminScraper, ScraperRegistry
from ats_scrapers.scrapers import peopleadmin as module
from ats_scrapers.scrapers.peopleadmin import (
    _parse_date,
    _parse_listing,
    _parse_slug,
    _posting_fields,
)

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py.

BASE = "https://acme.peopleadmin.com"
SEARCH = f"{BASE}/postings/search"
SEARCH_RE = re.compile(rf"^{re.escape(SEARCH)}\?page=\d+$")
PAGE_SIZE = 30
HEADERS = ("Posting Number", "Department", "Job Close Date")


def _item(
    number: int,
    title: str = "Librarian",
    *,
    kind: str = "posting",
    cells: tuple[str, ...] = ("F205", "Library", ""),
    summary: str = "Runs the reference desk.",
    responsive: bool = False,
) -> str:
    """One row of the search list, in either of the two layouts portals use."""
    path = "postings" if kind == "posting" else "pools"
    name = html.escape(title)
    suffix = " (Pool)" if kind == "pool" else ""
    attribute = f' data-posting-title="{name}"' if kind == "posting" else ""
    link = f'<h3><a href="/{path}/{number}">{name}</a>{suffix}</h3>'
    details = (
        f"<div class='details'><span class='job-description'>{html.escape(summary)}"
        f'</span><span class="job-actions"><a class="btn" href="/{path}/{number}">'
        f'View Details</a><a class="btn" href="/bookmarks?{path}_id={number}">'
        f"Bookmark</a></span></div>"
    )
    if responsive:
        values = "".join(
            "<div class='col-md-2 col-xs-12 job-title job-title-text-wrap "
            f"col-md-push-2'>{html.escape(cell)}</div>"
            for cell in cells
        )
        return (
            f"<div class='job-item job-item-{kind}'{attribute}>"
            "<div class='container-fluid'><div class='row'>"
            f"<div class='col-md-4 col-xs-12 job-title job-title-text-wrap'>{link}</div>"
            "<div class='col-md-8 col-xs-12 '><div class='col-md-2 col-xs-12'></div>"
            f"{values}</div></div>"
            f"<div class='row'><div class='col-md-12 col-xs-12'>{details}</div></div>"
            "</div></div>"
        )
    values = "".join(
        f'<div class="tbody-cell col-4 ">{html.escape(cell)}</div>' for cell in cells
    )
    return (
        f"<div class='job-item job-item-{kind}'{attribute}>"
        f'<div class="row row-table"><div class="job-title col-md-4">{link}</div>'
        f'<div class="col-md-8">{values}</div></div>'
        f"<div class=\"row row-table\"><div class='col-md-10'>{details}</div></div>"
        "</div>"
    )


def _header_row(
    headers: tuple[str, ...], *, roles: bool = True, title_link: bool = False
) -> str:
    role = ' role="columnheader"' if roles else ""
    title = (
        '<a class="sortable-column" href="/postings/search?sort=1+asc">Job Title</a>'
        if title_link
        else "&nbsp;"
    )
    cells = "".join(
        f"<div class='col-md-2 col-md-push-2'{role}>"
        f'<a class="sortable-column" href="/postings/search?sort={index + 2}+asc">'
        f"{html.escape(header)}</a></div>"
        for index, header in enumerate(headers)
    )
    return (
        "<div id='job_list_header_responsive' class=\"row hidden-xs\">"
        f"<div class='job-title col-md-4'{role}>{title}</div>"
        f"<div class='col-md-8'><div class='col-md-2'></div>{cells}</div></div>"
    )


def _search_page(
    items: list[str],
    *,
    total: int | None,
    page: int = 1,
    last: int = 1,
    headers: tuple[str, ...] = HEADERS,
    header_row: str | None = None,
) -> str:
    """A search page: pagination bar, result count, header row and rows."""
    if last > 1:
        following = (
            f'<a class="next_page" rel="next" href="/postings/search?page={page + 1}">'
            "Next</a>"
            if page < last
            else '<span class="next_page disabled">Next</span>'
        )
        bar = (
            '<div role="navigation" aria-label="Pagination" class="pagination">'
            f'<em class="current" aria-current="page">{page}</em>{following}</div>'
        )
    else:
        bar = ""
    count = (
        "<h2 id='search-results' class=\"result-section\">View Results "
        f"<span class='smaller muted'>({total}\n )</span></h2>"
        if total is not None
        else ""
    )
    return (
        "<html><head><title>Acme University Portal | Search Jobs</title>"
        "<script>var a = '<div class=\"job-item\">';</script></head><body>"
        f"{bar}{count}{header_row if header_row is not None else _header_row(headers)}"
        f'<div id="search_results">{"".join(items)}</div>'
        "<div id=\"footer\">Acme University</div></body></html>"
    )


def _serve(
    httpx_mock,
    items: list[str],
    *,
    total: int | None = None,
    announce_next: bool = True,
    **page_options: object,
) -> None:
    """Answer the search the way a portal does: thirty rows to a page, the
    count on every page, and the frame with no row past the end."""
    count = len(items) if total is None else total
    last = max(1, -(-len(items) // PAGE_SIZE))

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        chunk = items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return httpx.Response(
            200,
            text=_search_page(
                chunk,
                total=count,
                page=page,
                last=last if announce_next else 1,
                **page_options,
            ),
        )

    httpx_mock.add_callback(reply, url=SEARCH_RE, is_reusable=True)


def _pages(httpx_mock) -> list[int]:
    return [
        int(request.url.params["page"])
        for request in httpx_mock.get_requests()
        if request.url.path == "/postings/search"
    ]


def _posting_page(fields: list[tuple[str, str]], title: str = "Librarian") -> str:
    rows = "".join(
        f'<tr><th scope="row">{html.escape(label)}</th><td>{value}</td></tr>'
        for label, value in fields
    )
    return (
        f"<html><head><title>Acme University Portal | {html.escape(title)}</title>"
        "<style>th { color: red; }</style></head><body>"
        f"<h2>{html.escape(title)}</h2><h2>Posting Details</h2>"
        f"<table>{rows}</table>"
        "<h2>Supplemental Questions</h2><ol><li>Are you eligible?</li></ol>"
        "</body></html>"
    )


def _scraper(**options: object) -> PeopleAdminScraper:
    options.setdefault("include_descriptions", False)
    return PeopleAdminScraper("acme", **options)


# --- Registry and slug ------------------------------------------------------


def test_registry_resolves_peopleadmin() -> None:
    assert ScraperRegistry.get(ATSType.PEOPLEADMIN) is PeopleAdminScraper


@pytest.mark.parametrize(
    "slug",
    [
        "acme",
        " ACME ",
        "acme.peopleadmin.com",
        "https://acme.peopleadmin.com",
        "https://acme.peopleadmin.com/",
        "https://ACME.peopleadmin.com/postings/search?page=2",
        "https://acme.peopleadmin.com/postings/3968",
    ],
)
def test_slug_forms(slug: str) -> None:
    assert _parse_slug(slug) == "acme"
    assert PeopleAdminScraper(slug).base_url == BASE


def test_slug_keeps_dashes() -> None:
    assert _parse_slug("jobs-shepherd") == "jobs-shepherd"


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "   ",
        "-acme",
        "acme-",
        "acme/postings",
        "acme corp",
        "ac\nme",
        "acme.example.com",
        "district.ted.peopleadmin.com",
        "http://acme.peopleadmin.com",
        "https://peopleadmin.com/postings/search",
        "https://district.ted.peopleadmin.com/postings/search",
        "https://acme.peopleadmin.com.evil.com/postings/search",
        "https://evil.com/acme.peopleadmin.com",
        "https://user:pw@acme.peopleadmin.com/",
        "https://acme.peopleadmin.com:8443/",
    ],
)
def test_invalid_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(ScraperError):
        PeopleAdminScraper(slug)


# --- Search list ------------------------------------------------------------


def test_parses_a_listing(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [
            _item(3968, "Senior Librarian & Archivist", cells=("F205", "Library", "11/30/2026")),
            _item(3950, "Program Coordinator", cells=("S411", "Counseling", "")),
        ],
    )

    jobs = _scraper(company_name=" Acme University ").fetch()

    assert [job.title for job in jobs] == [
        "Senior Librarian & Archivist",
        "Program Coordinator",
    ]
    first = jobs[0]
    assert str(first.url) == f"{BASE}/postings/3968"
    assert first.company == "Acme University"
    assert first.ats_type is ATSType.PEOPLEADMIN
    assert first.ats_id == "acme:3968"
    assert first.global_id == "peopleadmin:acme:3968"
    assert first.requisition_id == "F205"
    assert first.department == "Library"
    assert first.description is None
    assert first.raw == {
        "kind": "posting",
        "columns": {
            "Posting Number": "F205",
            "Department": "Library",
            "Job Close Date": "11/30/2026",
        },
        "summary": "Runs the reference desk.",
        "close_date": "11/30/2026",
    }
    # An empty cell is no value.
    assert jobs[1].raw["columns"] == {"Posting Number": "S411", "Department": "Counseling"}
    assert "close_date" not in jobs[1].raw
    assert _pages(httpx_mock) == [1]


def test_company_falls_back_to_the_tenant_label(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1)])

    assert _scraper().fetch()[0].company == "acme"
    assert _scraper(company_name="   ").fetch()[0].company == "acme"


def test_reads_the_responsive_layout(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [_item(7967, "Recruiter", cells=("R7", "Partnerships", ""), responsive=True)],
        header_row=_header_row(HEADERS, roles=False),
    )

    job = _scraper().fetch()[0]

    assert job.title == "Recruiter"
    assert job.requisition_id == "R7"
    assert job.department == "Partnerships"


def test_the_title_column_header_is_not_a_column(httpx_mock) -> None:
    """Some portals make the title header a sort link too."""
    _serve(
        httpx_mock,
        [_item(1, cells=("F1", "Library", ""))],
        header_row=_header_row(HEADERS, title_link=True),
    )

    job = _scraper().fetch()[0]

    assert job.raw["columns"] == {"Posting Number": "F1", "Department": "Library"}


def test_cells_are_not_paired_with_headers_when_the_counts_differ(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1, cells=("F1", "Library"))])

    job = _scraper().fetch()[0]

    assert "columns" not in job.raw
    assert job.requisition_id is None
    assert job.department is None


def test_a_list_without_columns_still_reads(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1, cells=())], header_row="")

    job = _scraper().fetch()[0]

    assert job.title == "Librarian"
    assert job.raw == {"kind": "posting", "summary": "Runs the reference desk."}


def test_pools_are_read_as_listed(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [
            _item(16907, "Production Assistant"),
            _item(16992, "Non Student Tutor", kind="pool", cells=("", "Tutoring", "")),
            # A pool and a posting are numbered separately.
            _item(16992, "Dean of Students"),
        ],
    )

    jobs = _scraper().fetch()

    assert [(job.title, job.ats_id, job.raw["kind"]) for job in jobs] == [
        ("Production Assistant", "acme:16907", "posting"),
        ("Non Student Tutor (Pool)", "acme:pool-16992", "pool"),
        ("Dean of Students", "acme:16992", "posting"),
    ]
    assert str(jobs[1].url) == f"{BASE}/pools/16992"
    assert len({job.global_id for job in jobs}) == 3


def test_pools_can_be_left_out(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [_item(1), _item(2, "Adjunct Pool", kind="pool"), _item(3, "Registrar")],
    )

    jobs = _scraper(include_pools=False).fetch()

    assert [job.ats_id for job in jobs] == ["acme:1", "acme:3"]


# Opening dates the scraper must accept are well in the past: it drops the
# ones that are still to come, so they must not depend on the day the suite
# runs.


def test_column_values_fill_the_fields(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [_item(1, cells=("Fresno City College", "11/02/2024", "$24.50/hour", "12/01/2026"))],
        headers=("Campus Location", "Job Open Date", "Salary Range", "Close Date"),
    )

    job = _scraper().fetch()[0]

    assert job.location == "Fresno City College"
    assert job.posted_at == datetime(2024, 11, 2, tzinfo=UTC)
    assert job.salary_summary == "$24.50/hour"
    assert job.raw["close_date"] == "12/01/2026"
    # The closing hour and time zone are not given: no instant is derived.
    assert job.application_deadline is None


@pytest.mark.parametrize("value", ["N/A", "Other", "none", "TBD"])
def test_a_place_field_that_names_no_place_is_not_a_location(httpx_mock, value: str) -> None:
    _serve(httpx_mock, [_item(1, cells=(value,))], headers=("Location",))

    assert _scraper().fetch()[0].location is None


def test_a_close_date_that_is_not_a_date_is_not_kept(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [_item(1, cells=("F1", "Library", "Open Until Filled"))],
    )

    job = _scraper().fetch()[0]

    assert "close_date" not in job.raw
    assert job.raw["columns"]["Job Close Date"] == "Open Until Filled"


# --- Walk -------------------------------------------------------------------


def test_walks_every_page(httpx_mock) -> None:
    _serve(httpx_mock, [_item(n, f"Job {n}") for n in range(1, 76)])

    jobs = _scraper().fetch()

    assert [job.ats_id for job in jobs] == [f"acme:{n}" for n in range(1, 76)]
    # The last page has no "next" link: no request past it.
    assert _pages(httpx_mock) == [1, 2, 3]


def test_a_full_last_page_needs_no_further_request(httpx_mock) -> None:
    _serve(httpx_mock, [_item(n) for n in range(1, 61)])

    assert len(_scraper().fetch()) == 60
    assert _pages(httpx_mock) == [1, 2]


def test_reads_on_without_a_next_link_while_rows_are_announced(httpx_mock) -> None:
    """The count, not only the pagination bar, says there is more."""
    _serve(httpx_mock, [_item(n) for n in range(1, 41)], announce_next=False)

    assert len(_scraper().fetch()) == 40
    assert _pages(httpx_mock) == [1, 2]


def test_an_empty_portal_returns_no_job(httpx_mock) -> None:
    _serve(httpx_mock, [])

    assert _scraper().fetch() == []
    assert _pages(httpx_mock) == [1]


def test_a_count_larger_than_the_list_within_the_floor(httpx_mock) -> None:
    """A row closed between two pages: the count is one ahead of the list."""
    _serve(httpx_mock, [_item(n) for n in range(1, 30)], total=30)

    assert len(_scraper().fetch()) == 29
    # One request past the page, which answers no row.
    assert _pages(httpx_mock) == [1, 2]


def test_a_count_far_larger_than_the_list_is_an_error(httpx_mock) -> None:
    _serve(httpx_mock, [_item(n) for n in range(1, 11)], total=40)

    with pytest.raises(ScraperError, match="only 10 of the 40 rows"):
        _scraper().fetch()


def test_a_row_listed_on_two_pages_is_kept_once(httpx_mock) -> None:
    """A posting opened during the walk pushes a row onto the next page."""
    first = [_item(n) for n in range(1, 31)]
    second = [_item(30), _item(31)]
    httpx_mock.add_response(
        url=f"{SEARCH}?page=1", text=_search_page(first, total=31, page=1, last=2)
    )
    httpx_mock.add_response(
        url=f"{SEARCH}?page=2", text=_search_page(second, total=31, page=2, last=2)
    )

    jobs = _scraper().fetch()

    assert [job.ats_id for job in jobs] == [f"acme:{n}" for n in range(1, 32)]


def test_a_page_that_repeats_ends_the_walk(httpx_mock) -> None:
    """A portal that answers the same page whatever the number."""
    page = _search_page([_item(n) for n in range(1, 31)], total=90, page=1, last=3)
    httpx_mock.add_response(url=SEARCH_RE, text=page, is_reusable=True)

    with pytest.raises(ScraperError, match="only 30 of the 90 rows"):
        _scraper().fetch()
    assert _pages(httpx_mock) == [1, 2]


def test_a_row_without_a_posting_link_counts_against_the_floor(httpx_mock) -> None:
    broken = "<div class='job-item job-item-posting'><h3>Untitled</h3></div>"
    _serve(httpx_mock, [_item(n) for n in range(1, 10)] + [broken])

    assert len(_scraper().fetch()) == 9

    httpx_mock.reset()
    _serve(httpx_mock, [_item(n) for n in range(1, 9)] + [broken, broken.replace("Untitled", "Other")])

    with pytest.raises(ScraperError, match="only 8 of the 10 rows"):
        _scraper().fetch()


def test_a_page_of_unreadable_rows_does_not_pass_for_the_end(httpx_mock) -> None:
    def broken(n: int) -> str:
        return f"<div class='job-item job-item-posting'><h3>Row {n}</h3></div>"

    items = (
        [_item(n) for n in range(1, 31)]
        + [broken(n) for n in range(31, 61)]
        + [_item(n) for n in range(61, 91)]
    )
    _serve(httpx_mock, items)

    with pytest.raises(ScraperError, match="only 60 of the 90 rows"):
        _scraper().fetch()
    # The page of unreadable rows was not taken for the end of the list.
    assert _pages(httpx_mock) == [1, 2, 3]


def test_the_page_guard_refuses_a_partial_list(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_PAGES", 2)
    _serve(httpx_mock, [_item(n) for n in range(1, 91)])

    with pytest.raises(ScraperError, match="page guard 2"):
        _scraper().fetch()
    assert _pages(httpx_mock) == [1, 2]


def test_the_page_guard_allows_a_list_that_ends_on_it(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_PAGES", 2)
    _serve(httpx_mock, [_item(n) for n in range(1, 61)])

    assert len(_scraper().fetch()) == 60


def test_a_page_that_is_not_a_search_page_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{SEARCH}?page=1",
        text="<html><head><title>PowerSchool</title></head><body>Moved</body></html>",
    )

    with pytest.raises(ScraperError, match=r"not a search page.*PowerSchool"):
        _scraper().fetch()


def test_a_later_page_that_is_not_a_search_page_is_an_error(httpx_mock) -> None:
    first = _search_page([_item(n) for n in range(1, 31)], total=45, page=1, last=2)
    httpx_mock.add_response(url=f"{SEARCH}?page=1", text=first)
    httpx_mock.add_response(
        url=f"{SEARCH}?page=2", text="<html><title>Maintenance</title></html>"
    )

    with pytest.raises(ScraperError, match="page 2 is not a search page"):
        _scraper().fetch()


def test_a_missing_portal_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SEARCH}?page=1", status_code=404)

    with pytest.raises(CompanyNotFoundError):
        _scraper().fetch()


def test_a_failing_page_fails_the_scrape(httpx_mock) -> None:
    first = _search_page([_item(n) for n in range(1, 31)], total=45, page=1, last=2)
    httpx_mock.add_response(url=f"{SEARCH}?page=1", text=first)
    httpx_mock.add_response(url=f"{SEARCH}?page=2", status_code=500, is_reusable=True)

    with pytest.raises(ScraperError):
        _scraper().fetch()


def test_a_listing_without_a_count_is_walked_by_its_next_links(httpx_mock) -> None:
    first = _search_page([_item(n) for n in range(1, 31)], total=None, page=1, last=2)
    second = _search_page([_item(31)], total=None, page=2, last=2)
    httpx_mock.add_response(url=f"{SEARCH}?page=1", text=first)
    httpx_mock.add_response(url=f"{SEARCH}?page=2", text=second)

    assert len(_scraper().fetch()) == 31


def test_markup_inside_a_script_is_not_a_row() -> None:
    listing = _parse_listing(_search_page([_item(1)], total=1))

    assert listing.is_search_page
    assert listing.total == 1
    assert listing.identities == ["1"]
    assert listing.title == "Acme University Portal | Search Jobs"


def test_a_count_with_a_thousands_separator() -> None:
    assert _parse_listing(_search_page([], total="1,204")).total == 1204  # type: ignore[arg-type]


# --- Posting page -----------------------------------------------------------


def _serve_posting(httpx_mock, number: int, fields: list[tuple[str, str]], path: str = "postings") -> None:
    httpx_mock.add_response(
        url=f"{BASE}/{path}/{number}", text=_posting_page(fields), is_reusable=True
    )


def test_the_posting_page_gives_the_description_and_fields(httpx_mock) -> None:
    _serve(httpx_mock, [_item(211962, "FP&A Partner", cells=("", "", ""))])
    _serve_posting(
        httpx_mock,
        211962,
        [
            ("Open Date", "11/10/2024"),
            ("Requisition Number", "PRN46725B"),
            ("Department", "00286 - Budget &amp; Analysis"),
            ("Location", "Campus"),
            ("City", "Salt Lake City, UT"),
            ("Pay Rate Range", "$56,000-$130,000"),
            ("Salary", "$56,000-$130,000 dependent on level."),
            ("Close Date", "11/17/2026"),
            ("Preferences", "  "),
            (
                "Job Summary",
                "<p>Provides financial planning.</p><ul><li>Budgets</li><li>Forecasts</li></ul>"
                "<script>track()</script>",
            ),
        ],
    )

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.posted_at == datetime(2024, 11, 10, tzinfo=UTC)
    assert job.requisition_id == "PRN46725B"
    assert job.department == "00286 - Budget & Analysis"
    # The city rather than "Campus".
    assert job.location == "Salt Lake City, UT"
    assert job.salary_summary == "$56,000-$130,000 dependent on level."
    assert job.raw["close_date"] == "11/17/2026"
    assert job.description == (
        "Open Date\n11/10/2024\n\n"
        "Requisition Number\nPRN46725B\n\n"
        "Department\n00286 - Budget & Analysis\n\n"
        "Location\nCampus\n\n"
        "City\nSalt Lake City, UT\n\n"
        "Pay Rate Range\n$56,000-$130,000\n\n"
        "Salary\n$56,000-$130,000 dependent on level.\n\n"
        "Close Date\n11/17/2026\n\n"
        "Job Summary\nProvides financial planning.\nBudgets\nForecasts"
    )


def test_the_list_columns_come_before_the_posting_page(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1, cells=("F205", "Library", ""))])
    _serve_posting(
        httpx_mock, 1, [("Posting Number", "OTHER"), ("Department", "Other"), ("Campus", "North")]
    )

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.requisition_id == "F205"
    assert job.department == "Library"
    assert job.location == "North"


def test_the_more_specific_place_wins_wherever_it_is_given(httpx_mock) -> None:
    """The list says "Main"; the posting page names the city."""
    _serve(httpx_mock, [_item(1, cells=("Main",))], headers=("Campus",))

    assert _scraper().fetch()[0].location == "Main"

    _serve_posting(httpx_mock, 1, [("City", "Salt Lake City, UT"), ("Campus", "Other")])

    assert _scraper(include_descriptions=True).fetch()[0].location == "Salt Lake City, UT"


def test_an_opening_date_in_the_future_is_not_a_posting_date(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1), _item(2, "Registrar")])
    _serve_posting(httpx_mock, 1, [("Open Date", "01/11/2099")])
    _serve_posting(
        httpx_mock, 2, [("Open Date", "08/09/2099"), ("Posting Date", "11/02/2024")]
    )

    jobs = _scraper(include_descriptions=True).fetch()

    assert jobs[0].posted_at is None
    # The next label that gives a date that has come.
    assert jobs[1].posted_at == datetime(2024, 11, 2, tzinfo=UTC)


def test_a_pool_page_is_read_like_a_posting_page(httpx_mock) -> None:
    _serve(httpx_mock, [_item(16992, "Non Student Tutor", kind="pool", cells=("", "", ""))])
    _serve_posting(
        httpx_mock,
        16992,
        [("Pool Number", ""), ("Open Date", "07/01/2024"), ("Job Description", "Tutor students.")],
        path="pools",
    )

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.posted_at == datetime(2024, 7, 1, tzinfo=UTC)
    assert job.requisition_id is None
    assert job.description == "Open Date\n07/01/2024\n\nJob Description\nTutor students."


def test_a_posting_page_that_fails_keeps_the_posting(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1), _item(2, "Registrar")])
    httpx_mock.add_response(url=f"{BASE}/postings/1", status_code=500, is_reusable=True)
    _serve_posting(httpx_mock, 2, [("Job Description", "Keeps the records.")])

    jobs = _scraper(include_descriptions=True).fetch()

    assert [job.description for job in jobs] == [None, "Job Description\nKeeps the records."]


def test_a_removed_posting_answers_the_list_and_gets_no_description(httpx_mock) -> None:
    """A posting closed since the list was read redirects to the list."""
    _serve(httpx_mock, [_item(1)])
    httpx_mock.add_response(
        url=f"{BASE}/postings/1", text=_search_page([_item(2, "Registrar")], total=1)
    )

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description is None
    assert job.title == "Librarian"


def test_the_description_is_capped(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_DESCRIPTION_CHARS", 40)
    _serve(httpx_mock, [_item(1)])
    _serve_posting(httpx_mock, 1, [("Job Description", "word " * 50)])

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description is not None
    assert len(job.description) <= 40
    assert job.description.startswith("Job Description\nword word")


def test_get_description_fetches_one_posting(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1)])
    _serve_posting(httpx_mock, 1, [("Job Description", "Runs the desk.")])
    scraper = _scraper()
    job = scraper.fetch()[0]
    assert job.description is None

    assert scraper.get_description(job) == "Job Description\nRuns the desk."
    assert scraper.get_description(job) == "Job Description\nRuns the desk."
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/postings/1"]) == 1


def test_a_table_pasted_into_a_field_stays_in_that_field() -> None:
    page = _posting_page(
        [
            (
                "Duties",
                "<p>See the table.</p><table><tr><th>Task</th><td>Share</td></tr>"
                "<tr><th>Teaching</th><td>60%</td></tr></table>",
            ),
            ("Rank", "Instructor"),
        ]
    )

    assert _posting_fields(page) == [
        ("Duties", "See the table.\nTask\nShare\nTeaching\n60%"),
        ("Rank", "Instructor"),
    ]


def test_a_stray_end_tag_in_a_field_does_not_close_the_table() -> None:
    page = _posting_page(
        [
            ("Summary", "Pasted text</div></span></section> and the rest"),
            ("Rank", "Instructor"),
            ("Department", "Library"),
        ]
    )

    assert _posting_fields(page) == [
        ("Summary", "Pasted text and the rest"),
        ("Rank", "Instructor"),
        ("Department", "Library"),
    ]


def test_end_tags_left_out_inside_a_field_do_not_swallow_the_next_row() -> None:
    page = _posting_page(
        [("Summary", "<p>First<p>Second <b>bold"), ("Rank", "Instructor")]
    )

    assert _posting_fields(page) == [
        ("Summary", "First\nSecond bold"),
        ("Rank", "Instructor"),
    ]


def test_deeply_nested_markup_in_a_field_is_read() -> None:
    nested = "<span>" * 5000 + "Deep text" + "</span>" * 5000
    page = _posting_page([("Summary", nested), ("Rank", "Instructor")])

    assert _posting_fields(page) == [("Summary", "Deep text"), ("Rank", "Instructor")]


def test_a_row_without_a_label_or_a_value_is_not_a_field() -> None:
    page = (
        "<table><tr><td>No label</td></tr><tr><th>Only a label</th></tr>"
        "<tr><th> </th><td>Blank label</td></tr>"
        "<tr><th>Rank</th><td><br></td></tr>"
        "<tr><th>Title</th><td>Dean</td></tr></table>"
    )

    assert _posting_fields(page) == [("Title", "Dean")]


def test_a_page_without_fields_gives_no_description() -> None:
    assert _posting_fields("<html><body><p>Nothing here</p></body></html>") == []
    assert _posting_fields("") == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("11/09/2026", datetime(2026, 11, 9, tzinfo=UTC)),
        (" 1/5/2027 ", datetime(2027, 1, 5, tzinfo=UTC)),
        ("13/40/2026", None),
        ("2026-11-09", None),
        ("Open Until Filled", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(value: str | None, expected: datetime | None) -> None:
    assert _parse_date(value) == expected
