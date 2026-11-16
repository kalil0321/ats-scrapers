"""Tests for the Job Bank Canada scraper.

The scraper parses server-rendered HTML — no JSON API. Fixtures here
mirror the live ``jobsearch?searchstring=&sort=D&page=N`` markup so
parsing regressions surface immediately.
"""

from __future__ import annotations

import re
from collections import Counter

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import JobBankCAScraper, ScraperRegistry
from ats_scrapers.scrapers.jobbankca import (
    SEARCH_URL,
    _infer_country_iso,
    _infer_is_remote,
    _parse_date,
)

_SEARCH_RE = re.compile(r"^https://www\.jobbank\.gc\.ca/jobsearch/jobsearch")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.jobbankca as jb
    monkeypatch.setattr(jb, "MAX_RETRIES", 2)
    monkeypatch.setattr(jb, "RETRY_BASE_DELAY", 0.0)


def _article(
    *,
    job_id: str = "49482951",
    title: str = "electrician, industrial",
    business: str = "Kristian Electric Ltd",
    location: str = "Calgary, AB",
    date: str = "May 08, 2026",
    salary: str = "$42.00 to $50.00 hourly (to be negotiated)",
    telework: str | None = None,
    appmethod: str = "Direct Apply",
    new_flag: bool = True,
    posted_on_jb: bool = True,
    job_number: str | None = "3571211",
    posting_path: str = "jobposting",
) -> str:
    flags = []
    if new_flag:
        flags.append('<span class="new">New</span>')
    if telework:
        flags.append(f'<span class="telework">{telework}</span>')
    flags.append(f'<span class="appmethod">{appmethod}</span>')
    if posted_on_jb:
        flags.append(
            '<span class="postedonJB">Posted on Job Bank'
            '<span class="description">info</span></span>'
        )
    flag_block = (
        f'<span class="flag">{"".join(flags)}</span>'
        if flags else ""
    )
    source_li = ""
    if job_number is not None:
        source_li = (
            '<li class="source">'
            '<span class="job-source job-source-icon-16">'
            '<span class="wb-inv">Job Bank</span></span>'
            '<span class="wb-inv">Job number:</span>'
            f'<span class="fa fa-hashtag"></span>{job_number}</li>'
        )
    return (
        f'<article id="article-{job_id}" class="action-buttons">'
        f'<a href="/jobsearch/{posting_path}/{job_id};jsessionid=ABC?source=searchresults"'
        ' class="resultJobItem">'
        f'<h3 class="title">{flag_block}'
        '<span class="job-source job-source-icon-16">'
        '<span class="wb-inv">Job Bank</span></span>'
        f'<span class="noctitle">{title}</span></h3>'
        '<ul class="list-unstyled">'
        f'<li class="date">{date}</li>'
        f'<li class="business">{business}</li>'
        '<li class="location">'
        '<span class="fas fa-map-marker-alt"></span> '
        '<span class="wb-inv">Location</span>'
        f'{location}</li>'
        f'<li class="salary"><span class="fa fa-dollar"></span>Salary {salary}</li>'
        f'{source_li}'
        '</ul></a></article>'
    )


def _page(articles: list[str], *, total: str = "62,139") -> str:
    body = "\n".join(articles)
    return (
        '<!DOCTYPE html><html><body>'
        f'<span class="found" id="results-count">{total}</span>'
        f'{body}'
        '</body></html>'
    )


# --- registry / wiring -------------------------------------------------------


def test_registry_resolves_jobbankca() -> None:
    assert ScraperRegistry.get(ATSType.JOBBANKCA) is JobBankCAScraper


def test_ats_enum_value() -> None:
    assert ATSType.JOBBANKCA.value == "jobbankca"


# --- parse_job: full happy-path ---------------------------------------------


def test_parses_full_article() -> None:
    """Every field on a typical article block round-trips into a Job."""
    chunk = _article(telework="On site")
    job = JobBankCAScraper("any")._parse_job(chunk)
    assert job is not None
    assert job.ats_type is ATSType.JOBBANKCA
    assert job.ats_id == "49482951"
    assert job.title == "electrician, industrial"
    assert job.company == "Kristian Electric Ltd"
    assert job.location == "Calgary, AB"
    assert str(job.url) == "https://www.jobbank.gc.ca/jobsearch/jobposting/49482951"
    assert job.country_iso == "CA"
    assert job.region == "North America"
    assert job.language == "en"
    assert job.salary_summary == "$42.00 to $50.00 hourly (to be negotiated)"
    assert job.salary_currency == "CAD"
    assert job.salary_period == "HOUR"
    assert job.is_remote is False  # "On site" → explicit no
    assert job.commitment == "On site"
    assert job.requisition_id == "3571211"  # Job number, distinct from ats_id
    assert job.posted_at is not None
    assert job.posted_at.year == 2026
    assert job.posted_at.month == 5
    assert job.posted_at.day == 8
    assert job.raw is not None
    assert job.raw.get("application_method") == "Direct Apply"
    assert job.raw.get("posted_on_job_bank") is True
    assert job.raw.get("new_flag") is True
    assert job.raw.get("jobbank_number") == "3571211"


def test_global_id_uses_jobbankca_prefix() -> None:
    job = JobBankCAScraper("any")._parse_job(_article(job_id="123"))
    assert job is not None
    assert job.global_id == "jobbankca:123"


def test_preserves_temporary_foreign_worker_posting_path() -> None:
    job = JobBankCAScraper("any")._parse_job(
        _article(job_id="123", posting_path="jobpostingtfw")
    )
    assert job is not None
    assert str(job.url) == (
        "https://www.jobbank.gc.ca/jobsearch/jobpostingtfw/123"
    )


# --- parse_job: variations --------------------------------------------------


def test_strips_location_wb_inv_label() -> None:
    """The wb-inv 'Location' label collapses next to the city after
    tag-stripping. Make sure the leading prefix gets cut."""
    job = JobBankCAScraper("any")._parse_job(
        _article(location="Burlington (ON)")
    )
    assert job is not None
    assert job.location == "Burlington (ON)"
    assert job.country_iso == "CA"


def test_telework_remote_marks_is_remote_true() -> None:
    job = JobBankCAScraper("any")._parse_job(_article(telework="Remote"))
    assert job is not None
    assert job.is_remote is True
    assert job.commitment == "Remote"


def test_hybrid_telework_leaves_is_remote_none() -> None:
    job = JobBankCAScraper("any")._parse_job(_article(telework="Hybrid"))
    assert job is not None
    assert job.is_remote is None  # Hybrid: noncommittal.


def test_missing_telework_falls_back_to_title_keyword() -> None:
    job = JobBankCAScraper("any")._parse_job(
        _article(title="Senior Engineer (Remote)", telework=None)
    )
    assert job is not None
    assert job.is_remote is True


def test_non_dollar_salary_keeps_summary_drops_currency() -> None:
    """A salary string without a ``$`` shouldn't claim CAD."""
    chunk = _article(salary="To be negotiated")
    job = JobBankCAScraper("any")._parse_job(chunk)
    assert job is not None
    assert job.salary_summary == "To be negotiated"
    assert job.salary_currency is None


@pytest.mark.parametrize(
    ("salary", "period"),
    [
        ("$200 daily", "DAY"),
        ("$1,200 weekly", "WEEK"),
        ("$5,000 monthly", "MONTH"),
        ("$80,000 annually", "YEAR"),
        ("$25 par heure", "HOUR"),
    ],
)
def test_salary_period_preserves_source_cadence(salary, period) -> None:
    job = JobBankCAScraper("any")._parse_job(_article(salary=salary))
    assert job is not None
    assert job.salary_currency == "CAD"
    assert job.salary_period == period


def test_unknown_salary_cadence_does_not_emit_structured_currency() -> None:
    job = JobBankCAScraper("any")._parse_job(_article(salary="$42 negotiable"))
    assert job is not None
    assert job.salary_summary == "$42 negotiable"
    assert job.salary_currency is None
    assert job.salary_period is None


def test_job_number_omitted_when_same_as_ats_id() -> None:
    job = JobBankCAScraper("any")._parse_job(
        _article(job_id="3571211", job_number="3571211")
    )
    assert job is not None
    assert job.requisition_id == "3571211"
    # Don't duplicate the same id into raw — only when the two differ.
    assert (job.raw or {}).get("jobbank_number") is None


def test_missing_required_fields_returns_none() -> None:
    """Articles without a title are dropped silently."""
    chunk = '<article id="article-123" class="x"><div>broken</div></article>'
    assert JobBankCAScraper("any")._parse_job(chunk) is None


def test_french_language_constructor() -> None:
    """A scraper instantiated with ``language='fr'`` tags rows accordingly."""
    scraper = JobBankCAScraper("any", language="fr")
    job = scraper._parse_job(_article())
    assert job is not None
    assert job.language == "fr"


# --- _parse_page ------------------------------------------------------------


def test_parse_page_extracts_all_articles() -> None:
    html_text = _page([
        _article(job_id="111", title="Welder"),
        _article(job_id="222", title="Carpenter", business="Acme Co"),
        _article(job_id="333", title="Plumber"),
    ])
    jobs = JobBankCAScraper("any")._parse_page(html_text)
    assert [j.ats_id for j in jobs] == ["111", "222", "333"]
    assert [j.title for j in jobs] == ["Welder", "Carpenter", "Plumber"]


def test_parse_page_empty_returns_empty_list() -> None:
    """A search page past the last result has zero ``<article>`` blocks."""
    html_text = '<html><body><p>No results.</p></body></html>'
    assert JobBankCAScraper("any")._parse_page(html_text) == []


# --- pagination via fetch (httpx_mock) --------------------------------------


@pytest.fixture
def _two_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.jobbankca as jb
    monkeypatch.setattr(jb, "PAGE_SIZE", 2)


def _serve_pages(httpx_mock, pages: dict[int, list[str]]) -> Counter[int]:
    """Answer each page with its versions in turn, repeating the last one.

    A page with several versions stands for the index replicas that order
    postings differently. Returns how often each page was requested.
    """
    served: Counter[int] = Counter()

    def serve(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        versions = pages[page]
        served[page] += 1
        return httpx.Response(
            200, text=versions[min(served[page], len(versions)) - 1]
        )

    httpx_mock.add_callback(serve, url=_SEARCH_RE, is_reusable=True)
    return served


def _ids(*ids: str, total: str) -> str:
    return _page([_article(job_id=job_id) for job_id in ids], total=total)


def test_fetch_orders_results_by_posting_date(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SEARCH_URL,
        match_params={"searchstring": "", "sort": "D", "page": "1"},
        html=_ids("1", total="1"),
    )

    assert [job.ats_id for job in JobBankCAScraper("any").fetch()] == ["1"]


@pytest.mark.usefixtures("_two_per_page")
def test_fetch_reads_every_announced_page(httpx_mock) -> None:
    served = _serve_pages(
        httpx_mock,
        {
            1: [_ids("1001", "1002", total="5")],
            2: [_ids("2001", "2002", total="5")],
            3: [_ids("3001", total="5")],
        },
    )

    jobs = JobBankCAScraper("any").fetch()

    assert [job.ats_id for job in jobs] == ["1001", "1002", "2001", "2002", "3001"]
    assert served == {1: 1, 2: 1, 3: 1}


@pytest.mark.usefixtures("_two_per_page")
def test_fetch_rereads_pages_until_the_announced_count_is_reached(
    httpx_mock,
) -> None:
    """Page 2 first comes from a replica that orders 12 after 13 and repeats
    12, so 14 is skipped until the pages are read again."""
    served = _serve_pages(
        httpx_mock,
        {
            1: [_ids("11", "12", total="4")],
            2: [_ids("12", "13", total="4"), _ids("13", "14", total="4")],
        },
    )

    jobs = JobBankCAScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["11", "12", "13", "14"]
    assert served == {1: 2, 2: 2}


@pytest.mark.usefixtures("_two_per_page")
def test_fetch_fails_when_every_pass_stays_short(httpx_mock) -> None:
    _serve_pages(
        httpx_mock,
        {1: [_ids("11", "12", total="4")], 2: [_ids("12", "13", total="4")]},
    )

    with pytest.raises(
        ScraperError,
        match="collected 3 of the 4 announced results after 4 passes",
    ):
        JobBankCAScraper("any").fetch()


@pytest.mark.usefixtures("_two_per_page")
def test_fetch_accepts_postings_closed_during_the_scrape(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ats_scrapers.scrapers.jobbankca as jb

    monkeypatch.setattr(jb, "MIN_COVERAGE", 0.75)
    _serve_pages(
        httpx_mock,
        {1: [_ids("11", "12", total="4")], 2: [_ids("13", total="4")]},
    )

    assert len(JobBankCAScraper("any").fetch()) == 3


@pytest.mark.usefixtures("_two_per_page")
def test_fetch_follows_a_count_that_grows_between_passes(httpx_mock) -> None:
    served = _serve_pages(
        httpx_mock,
        {
            1: [_ids("11", "12", total="4"), _ids("11", "12", total="5")],
            2: [_ids("12", "13", total="5"), _ids("13", "14", total="5")],
            3: [_ids("15", total="5")],
        },
    )

    jobs = JobBankCAScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["11", "12", "13", "14", "15"]
    assert served == {1: 2, 2: 2, 3: 1}


@pytest.mark.usefixtures("_two_per_page")
def test_failed_page_fails_the_scrape(httpx_mock) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 2:
            return httpx.Response(500)
        return httpx.Response(200, text=_ids(f"{page}1", f"{page}2", total="8"))

    httpx_mock.add_callback(serve, url=_SEARCH_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="returned 500 on page=2"):
        JobBankCAScraper("any").fetch()


def test_fetch_respects_max_pages_cap(httpx_mock) -> None:
    """``max_pages=1`` stops after a single page even if more remain."""
    served = _serve_pages(httpx_mock, {1: [_ids("42", total="62,139")]})

    jobs = JobBankCAScraper("any", max_pages=1).fetch()

    assert [j.ats_id for j in jobs] == ["42"]
    assert served == {1: 1}


@pytest.mark.usefixtures("_two_per_page")
def test_bounded_fetch_reads_its_pages_once(httpx_mock) -> None:
    served = _serve_pages(
        httpx_mock,
        {1: [_ids("11", "12", total="40")], 2: [_ids("12", "13", total="40")]},
    )

    jobs = JobBankCAScraper("any", max_pages=2).fetch()

    assert sorted(job.ats_id for job in jobs) == ["11", "12", "13"]
    assert served == {1: 1, 2: 1}


def test_default_full_run_fails_at_safety_limit(
    httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ats_scrapers.scrapers.jobbankca as jobbank

    monkeypatch.setattr(jobbank, "MAX_PAGES", 1)
    httpx_mock.add_response(url=_SEARCH_RE, html=_page([_article(job_id="42")]))
    with pytest.raises(ScraperError, match="safety limit"):
        JobBankCAScraper("any").fetch()


def test_page_without_an_announced_count_raises(httpx_mock) -> None:
    html_text = _page([_article(job_id="1")]).replace(
        '>62,139<', '><'
    )
    httpx_mock.add_response(url=_SEARCH_RE, html=html_text)

    with pytest.raises(ScraperError, match="announced no result count"):
        JobBankCAScraper("any").fetch()


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('<span class="found" id="results-count">68,763</span> results', 68763),
        ('<span class="found" id="results-count"> 68 763 </span>', 68763),
        ('<span id="results-count">0</span>', 0),
        ('<span id="results-count"></span>', None),
        ("<html></html>", None),
    ],
)
def test_announced_total(markup: str, expected: int | None) -> None:
    from ats_scrapers.scrapers.jobbankca import _announced_total

    assert _announced_total(markup) == expected


def test_unparseable_articles_raise_instead_of_ending(httpx_mock) -> None:
    malformed = (
        '<span class="found" id="results-count">1</span>'
        '<article id="article-42"><div>changed markup</div></article>'
    )
    httpx_mock.add_response(url=_SEARCH_RE, html=malformed)
    with pytest.raises(ScraperError, match="1 articles but parsed 0"):
        JobBankCAScraper("any").fetch()


def test_mixed_valid_and_unparseable_articles_raise(httpx_mock) -> None:
    html_text = _page([_article(job_id="1")]).replace(
        "</body>",
        '<article id="article-2"><div>changed markup</div></article></body>',
    )
    httpx_mock.add_response(url=_SEARCH_RE, html=html_text)

    with pytest.raises(ScraperError, match="2 articles but parsed 1"):
        JobBankCAScraper("any").fetch()


def test_full_catalogue_rejects_empty_first_page(httpx_mock) -> None:
    httpx_mock.add_response(url=_SEARCH_RE, html=_page([]))

    with pytest.raises(ScraperError, match="returned no jobs"):
        JobBankCAScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_SEARCH_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        JobBankCAScraper("any").fetch()


def test_block_page_with_no_result_marker_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_SEARCH_RE, html="<html>blocked</html>")
    with pytest.raises(ScraperError, match="lacked result markup"):
        JobBankCAScraper("any").fetch()


# --- module-level helpers ---------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("May 08, 2026", (2026, 5, 8)),
    ("January 1, 2026", (2026, 1, 1)),
    ("Dec 31, 2025", (2025, 12, 31)),
    ("May 08, 2026 Expires May 22, 2026", (2026, 5, 8)),
])
def test_parse_date_handles_english_formats(
    text: str, expected: tuple[int, int, int]
) -> None:
    d = _parse_date(text)
    assert d is not None
    assert (d.year, d.month, d.day) == expected


@pytest.mark.parametrize("text", [
    None, "", "garbage",
])
def test_parse_date_returns_none_for_unparseable(text: str | None) -> None:
    assert _parse_date(text) is None


def test_parse_date_handles_french_format() -> None:
    parsed = _parse_date("08 mai 2026")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 5, 8)
    assert parsed.tzinfo is not None


def test_infer_is_remote_explicit_telework() -> None:
    assert _infer_is_remote("Remote", "x") is True
    assert _infer_is_remote("Telework", "x") is True
    assert _infer_is_remote("On site", "x") is False
    assert _infer_is_remote("Hybrid", "x") is None
    assert _infer_is_remote("Hybrid", "Remote Support Engineer") is None
    assert _infer_is_remote(None, "Senior Engineer") is None
    assert _infer_is_remote(None, "Senior Engineer (Remote)") is True


@pytest.mark.parametrize("location,expected", [
    ("Calgary, AB", "CA"),
    ("Burlington (ON)", "CA"),
    ("Toronto, Canada", "CA"),
    (None, "CA"),  # Job Bank is Canada-only.
    ("Whitehorse, YT", "CA"),
])
def test_infer_country_iso(location: str | None, expected: str) -> None:
    assert _infer_country_iso(location) == expected
