"""Tests for the Programathor (Brazil) scraper.

The site is HTML-only and geo-blocks non-Brazilian IPs (403 without
proxy). These tests exercise:

- Listing-card parsing — every Programathor field maps to the right
  Job slot
- Brazilian salary/location parsing (R$,
  "Remoto" → "Remote, Brazil")
- Pagination termination (3 consecutive duplicate-only pages → stop)
- The proxy URL helper (the host:port:user:pass form converts to
  the standard URL form httpx wants)
- 403 → ScraperError that says how to fix it
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ProgramathorScraper, ScraperRegistry
from ats_scrapers.scrapers.programathor import (
    _parse_brl_amount,
    _parse_salary,
    _resolve_proxy_url,
)

_LISTING_RE = re.compile(r"^https://programathor\.com\.br/jobs\?page=\d+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.programathor as p
    monkeypatch.setattr(p, "MAX_RETRIES", 1)
    monkeypatch.setattr(p, "RETRY_BASE_DELAY", 0.0)


def _empty_listing() -> str:
    return "<html><body><div class='wrapper-jobs-list'></div></body></html>"


def _card(
    *,
    job_id: str,
    title: str,
    company: str = "Acme",
    location: str = "Remoto",
    company_type: str = "Startup",
    salary: str = "Até R$5.000",
    contract: str = "PJ",
    skills: list[str] | None = None,
    new_label: bool = True,
) -> str:
    skills = skills or ["Python", "Django"]
    skill_html = "".join(
        f"<span class='tag-list background-gray'>{s}</span>" for s in skills
    )
    label = "<span class='new-label'>NOVA</span>" if new_label else ""
    slug_safe = re.sub(r"[^a-z0-9-]", "-", title.lower().replace(" ", "-"))
    return (
        f'<div class="cell-list ">'
        f'<a href="/jobs/{job_id}-{slug_safe}">'
        f'<div class="row">'
        f'<div class="col-sm-9">'
        f'<div class="cell-list-content">'
        f'<h3 class="text-24 line-height-30">{title}{label}</h3>'
        f'<div class="cell-list-content-icon">'
        f"<span><i class='fa fa-briefcase'></i>{company}</span>"
        f"<span><i class='fas fa-map-marker-alt'></i>{location}</span>"
        f"<span><i class='fa fa-building'></i>{company_type}</span>"
        f"<span><i class='far fa-money-bill-alt'></i>{salary}</span>"
        f"<span><i class='far fa-file-alt'></i>{contract}</span>"
        f"</div>"
        f"<div>{skill_html}</div>"
        f"</div></div></div></a></div>"
    )


def _listing(cards: list[str]) -> str:
    return f"<html><body><div class='wrapper-jobs-list'>{''.join(cards)}</div></body></html>"


def _detail(description: str = "<p>Build great systems.</p>") -> str:
    return f"<html><body><div class='job-description'>{description}</div></body></html>"


# --- proxy URL helper -------------------------------------------------------


def test_proxy_url_quad_colon_format_converted() -> None:
    """Some residential-proxy providers ship credentials in a
    ``host:port:user:pass`` shape (4 colons) instead of the standard
    ``http://user:pass@host:port`` URL — convert it for httpx."""
    out = _resolve_proxy_url("http://proxy.example.com:1000:alice:secret")
    assert out == "http://alice:secret@proxy.example.com:1000"


def test_proxy_url_already_canonical_passes_through() -> None:
    out = _resolve_proxy_url("http://user:pw@host.example:8080")
    assert out == "http://user:pw@host.example:8080"


def test_proxy_url_none_or_empty_returns_none() -> None:
    assert _resolve_proxy_url(None) is None
    assert _resolve_proxy_url("") is None


# --- registry ---------------------------------------------------------------


def test_registry_resolves_programathor() -> None:
    assert ScraperRegistry.get(ATSType.PROGRAMATHOR) is ProgramathorScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_listing_card(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs?page=1",
        text=_listing([_card(
            job_id="33458",
            title="Engenheiro QA Python",
            company="Chronos Cap",
            location="Remoto",
            company_type="Startup",
            salary="R$3.000 - R$5.000",
            contract="PJ",
            skills=["Python", "API", "SQL"],
        )]),
    )
    # Pages 2-4 all empty → 3 consecutive duplicate-only pages → stop.
    httpx_mock.add_response(
        url=re.compile(r"^https://programathor\.com\.br/jobs\?page=[2-9]$"),
        text=_empty_listing(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs/33458-engenheiro-qa-python",
        text=_detail("<p>Build <strong>Brazilian</strong> platforms.</p>"),
    )

    jobs = ProgramathorScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.PROGRAMATHOR
    assert j.ats_id == "33458"
    assert j.title == "Engenheiro QA Python"
    assert j.company == "Chronos Cap"
    assert j.location == "Remote, Brazil"
    assert j.is_remote is True
    assert j.salary_currency == "BRL"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 3000.0
    assert j.salary_max == 5000.0
    assert j.employment_type == "CONTRACT"  # PJ → CONTRACT
    assert j.commitment == "PJ"
    assert j.description == "Build Brazilian platforms."
    assert j.raw is not None
    assert j.raw["skills"] == ["Python", "API", "SQL"]
    assert j.raw["company_type"] == "Startup"
    assert str(j.url) == "https://programathor.com.br/jobs/33458-engenheiro-qa-python"


def test_meta_description_is_only_a_preview(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs?page=1",
        text=_listing([_card(job_id="7", title="Backend Engineer")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://programathor\.com\.br/jobs\?page=[2-9]$"),
        text=_empty_listing(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs/7-backend-engineer",
        text=(
            "<html><head><meta content='Build Brazilian APIs.' "
            "property='og:description'></head></html>"
        ),
    )

    jobs = ProgramathorScraper("any").fetch()

    assert jobs[0].description is None


def test_strips_new_label_from_title(httpx_mock) -> None:
    """Programathor injects a 'NOVA' tag inside the <h3>; strip it
    rather than ship 'Foo BarNOVA' as the title."""
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs?page=1",
        text=_listing([_card(job_id="1", title="Backend Engineer", new_label=True)]),
    )
    # Pages 2-4 all empty → 3 consecutive duplicate-only pages → stop.
    httpx_mock.add_response(
        url=re.compile(r"^https://programathor\.com\.br/jobs\?page=[2-9]$"),
        text=_empty_listing(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs/1-backend-engineer",
        text=_detail(),
    )
    jobs = ProgramathorScraper("any").fetch()
    assert jobs[0].title == "Backend Engineer"


def test_location_with_city_appends_brazil(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs?page=1",
        text=_listing([_card(job_id="1", title="X", location="São Paulo")]),
    )
    # Pages 2-4 all empty → 3 consecutive duplicate-only pages → stop.
    httpx_mock.add_response(
        url=re.compile(r"^https://programathor\.com\.br/jobs\?page=[2-9]$"),
        text=_empty_listing(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs/1-x",
        text=_detail(),
    )
    jobs = ProgramathorScraper("any").fetch()
    assert jobs[0].location == "São Paulo, Brazil"
    assert jobs[0].is_remote is False


# --- captured listing ---------------------------------------------------------

# The job-list column of /jobs?page=1, 2 and 3, captured on 18 Nov 2026 when
# the board had 30 open roles. Kept as served, except the signed query strings
# of the S3 logo URLs, which are removed. Page 3 is what the site serves past
# the last page.
FIXTURES = Path(__file__).parent / "fixtures" / "programathor"


def test_captured_listing_yields_every_open_role(httpx_mock) -> None:
    for page in (1, 2, 3):
        httpx_mock.add_response(
            url=f"https://programathor.com.br/jobs?page={page}",
            text=(FIXTURES / f"jobs_page_{page}.html").read_text(encoding="utf-8"),
        )
    httpx_mock.add_response(
        url=re.compile(r"^https://programathor\.com\.br/jobs\?page=[45]$"),
        text=(FIXTURES / "jobs_page_3.html").read_text(encoding="utf-8"),
        is_reusable=True,
    )

    jobs = ProgramathorScraper("any", include_descriptions=False).fetch()

    assert len(jobs) == 30
    by_id = {job.ats_id: job for job in jobs}
    assert len(by_id) == 30
    assert by_id["33884"].title == "Analista SAP B1 Pleno"
    assert by_id["33860"].title == "Desenvolvedor Full-Stack Estágio"
    assert by_id["33884"].is_remote is False
    assert by_id["33685"].title == "Desenvolvedor Back-End Senior (AI Engineer)"
    assert by_id["33685"].company == "LOLDESIGN Soluções Digitais LTDA"
    assert by_id["33685"].is_remote is True
    assert all("PRESENCIAL" not in job.title for job in jobs)


# --- salary parsing --------------------------------------------------------


def test_parse_brl_amount_handles_brazilian_thousand_separator() -> None:
    assert _parse_brl_amount("3.000") == 3000.0
    assert _parse_brl_amount("3.500,50") == 3500.50
    assert _parse_brl_amount("12.000") == 12000.0
    assert _parse_brl_amount("0") is None


@pytest.mark.parametrize("raw, expected", [
    ("Até R$5.000", (None, 5000.0, "BRL")),
    ("R$3.000 - R$5.000", (3000.0, 5000.0, "BRL")),
    ("A partir de R$8.000", (8000.0, None, "BRL")),
    ("A combinar", (None, None, None)),
    ("", (None, None, None)),
])
def test_parse_salary_shapes(raw: str, expected: tuple) -> None:
    assert _parse_salary(raw) == expected


# --- pagination termination --------------------------------------------------


def test_stops_after_three_consecutive_duplicate_pages(httpx_mock) -> None:
    """Real Programathor pagination doesn't 404 past the live tail —
    it just keeps returning the same id range. We stop after 3
    consecutive pages that yield 0 new ids."""
    page1 = _listing([_card(job_id="100", title="A"), _card(job_id="101", title="B")])
    # Pages 2-4 return the same two cards → all duplicates, increment
    # the empty-streak counter to 3 and stop.
    httpx_mock.add_response(url="https://programathor.com.br/jobs?page=1", text=page1)
    for p in (2, 3, 4):
        httpx_mock.add_response(
            url=f"https://programathor.com.br/jobs?page={p}", text=page1,
        )
    httpx_mock.add_response(url="https://programathor.com.br/jobs/100-a", text=_detail())
    httpx_mock.add_response(url="https://programathor.com.br/jobs/101-b", text=_detail())
    # Page 5 should NEVER be requested — if it is, httpx_mock will
    # error on the un-stubbed call.
    jobs = ProgramathorScraper("any", max_pages=20).fetch()
    assert {j.ats_id for j in jobs} == {"100", "101"}


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even when every page returns fresh ids, ``max_pages`` is the
    hard ceiling (so a buggy site can't run forever)."""
    # Each page returns one new id (no duplicates) — only the ``max_pages``
    # cap can stop us.
    for p in range(1, 6):
        httpx_mock.add_response(
            url=f"https://programathor.com.br/jobs?page={p}",
            text=_listing([_card(job_id=str(p * 100), title=f"Job {p}")]),
        )
        httpx_mock.add_response(
            url=f"https://programathor.com.br/jobs/{p * 100}-job-{p}",
            text=_detail(),
        )
    jobs = ProgramathorScraper("any", max_pages=5).fetch()
    assert len(jobs) == 5


# --- 403 / proxy handling ---------------------------------------------------


def test_403_raises_with_proxy_hint(httpx_mock) -> None:
    """The site returns 403 to non-BR IPs. Surfacing that as a generic
    ScraperError isn't enough — the message must point users at the
    PROXY env / proxy_url constructor argument."""
    httpx_mock.add_response(
        url="https://programathor.com.br/jobs?page=1", status_code=403,
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="geo-blocks"):
        ProgramathorScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE, status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        ProgramathorScraper("any").fetch()


@pytest.mark.parametrize("defer", [True, False])
def test_structured_detail_is_read_once(httpx_mock, defer) -> None:
    card = _card(job_id="7", title="Engineer", salary="A combinar")
    httpx_mock.add_response(url=_LISTING_RE, text=_listing([card, card]))
    httpx_mock.add_response(url="https://programathor.com.br/jobs/7-engineer", text="""
      <meta name="description" content="Build...">
      <script type="application/ld+json">{"@graph":[{"@type":"JobPosting",
        "description":"<h3>Habilidades</h3><p>C and C++</p>"}]}</script>
    """)
    scraper = ProgramathorScraper("any", max_pages=1, include_descriptions=not defer)
    job, = scraper.fetch()
    if defer:
        assert job.description is None and len(httpx_mock.get_requests()) == 1
    assert scraper.get_description(job) == "Habilidades\nC and C++"
    assert len(httpx_mock.get_requests()) == 2


def test_visible_sections_preserve_company_duties_requirements_and_skills(httpx_mock) -> None:
    scraper = ProgramathorScraper("any")
    job, = scraper._parse_listing(_listing([_card(job_id="7", title="Engineer")]))
    httpx_mock.add_response(url=str(job.url), text="""
      <div class="wrapper-header-job-show"><span class="tag tag-hover">C++</span></div>
      <div class="wrapper-content-job-show"><div class="line-height-2-4">
      <h3>Descrição da empresa</h3><p>Nossa empresa</p><h3>Requisitos</h3>
      <p>Compiladores &amp; redes</p></div><div>Related jobs</div></div>""")
    assert scraper.get_description(job) == (
        "Habilidades\nC++\nDescrição da empresa\nNossa empresa\nRequisitos\nCompiladores & redes"
    )


@pytest.mark.parametrize(("label", "expected"), [
    ("Estágio", "internship"), ("Freelance", "independent_contractor"),
    ("Temporário", "temporary"), ("CLT", None), ("PJ", None),
])
def test_contract_label_preserves_engagement(label, expected):
    html = _listing([_card(job_id="123", title="Developer", contract=label)])
    job, = ProgramathorScraper("any")._parse_listing(html)
    assert job.engagement_type == expected
    assert job.commitment == label
