"""Tests for the Job Bank Canada scraper.

The scraper parses server-rendered HTML — no JSON API. Fixtures here
mirror the live ``jobsearch?searchstring=&sort=M&page=N`` markup so
parsing regressions surface immediately.
"""

from __future__ import annotations

import re

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import JobBankCAScraper, ScraperRegistry
from ats_scrapers.scrapers.jobbankca import (
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
        f'<a href="/jobsearch/jobposting/{job_id};jsessionid=ABC?source=searchresults"'
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


def _page(articles: list[str]) -> str:
    body = "\n".join(articles)
    return (
        '<!DOCTYPE html><html><body>'
        '<span class="found" id="results-count">62,139</span>'
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


def test_fetch_paginates_until_empty_page(httpx_mock) -> None:
    """The scraper iterates pages and stops at the first empty one."""
    httpx_mock.add_response(
        url="https://www.jobbank.gc.ca/jobsearch/jobsearch",
        match_params={"searchstring": "", "sort": "M", "page": "1"},
        html=_page([_article(job_id="1001"), _article(job_id="1002")]),
    )
    httpx_mock.add_response(
        url="https://www.jobbank.gc.ca/jobsearch/jobsearch",
        match_params={"searchstring": "", "sort": "M", "page": "2"},
        html=_page([_article(job_id="2001")]),
    )
    httpx_mock.add_response(
        url="https://www.jobbank.gc.ca/jobsearch/jobsearch",
        match_params={"searchstring": "", "sort": "M", "page": "3"},
        html=_page([]),  # empty — termination signal
    )
    jobs = JobBankCAScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1001", "1002", "2001"}


def test_fetch_deduplicates_repeated_ats_id(httpx_mock) -> None:
    """Same ats_id appearing on two consecutive pages (timing race) is
    only emitted once."""
    httpx_mock.add_response(
        url="https://www.jobbank.gc.ca/jobsearch/jobsearch",
        match_params={"searchstring": "", "sort": "M", "page": "1"},
        html=_page([_article(job_id="9999")]),
    )
    httpx_mock.add_response(
        url="https://www.jobbank.gc.ca/jobsearch/jobsearch",
        match_params={"searchstring": "", "sort": "M", "page": "2"},
        html=_page([_article(job_id="9999")]),
    )
    httpx_mock.add_response(
        url="https://www.jobbank.gc.ca/jobsearch/jobsearch",
        match_params={"searchstring": "", "sort": "M", "page": "3"},
        html=_page([]),
    )
    jobs = JobBankCAScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["9999"]


def test_fetch_respects_max_pages_cap(httpx_mock) -> None:
    """``max_pages=1`` stops after a single page even if more remain."""
    httpx_mock.add_response(
        url="https://www.jobbank.gc.ca/jobsearch/jobsearch",
        match_params={"searchstring": "", "sort": "M", "page": "1"},
        html=_page([_article(job_id="42")]),
    )
    # Page 2 must NOT be requested — httpx_mock would error if it is.
    jobs = JobBankCAScraper("any", max_pages=1).fetch()
    assert [j.ats_id for j in jobs] == ["42"]


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_SEARCH_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
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
    None, "", "garbage", "08 mai 2026",  # French not supported in this helper.
])
def test_parse_date_returns_none_for_unparseable(text: str | None) -> None:
    assert _parse_date(text) is None


def test_infer_is_remote_explicit_telework() -> None:
    assert _infer_is_remote("Remote", "x") is True
    assert _infer_is_remote("Telework", "x") is True
    assert _infer_is_remote("On site", "x") is False
    assert _infer_is_remote("Hybrid", "x") is None
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

