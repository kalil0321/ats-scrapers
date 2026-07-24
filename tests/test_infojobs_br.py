"""Tests for the InfoJobs Brasil scraper.

InfoJobs Brasil is a server-rendered ASP.NET site whose infinite-scroll
behavior is backed by a JSON fragment endpoint
(``/mf-publicarea/VacancyList/GetVacancyListFragment``). The fragment
payload carries the same card markup the SSR page does. These tests
exercise:

- Card parsing for every Job slot the scraper populates
- Brazilian salary parsing (``R$ 1.700,00 a R$ 2.000,00``, "Até",
  "A partir de", "A combinar")
- Modality → remote inference (Presencial / Híbrido / Home office)
- Date parsing (``YYYY/MM/DD HH:MM:SS`` exact form)
- Pagination termination (``eof: true`` or three consecutive
  duplicate-only pages)
- The ``url`` query param is set on the listing URL, not the
  fragment-endpoint URL (the backend reads ``page`` from the
  encoded value, not from its own request URL)
- ``page`` cap honors ``max_pages``
"""

from __future__ import annotations

import re
import urllib.parse

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import InfoJobsBrasilScraper, ScraperRegistry
from ats_scrapers.scrapers.infojobs_br import (
    _infer_remote,
    _parse_brazilian_date,
    _parse_brl_amount,
    _parse_salary,
    _set_query_param,
)

_FRAGMENT_RE = re.compile(
    r"^https://www\.infojobs\.com\.br/mf-publicarea/VacancyList/GetVacancyListFragment\?url="
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.infojobs_br as ij
    monkeypatch.setattr(ij, "MAX_RETRIES", 1)
    monkeypatch.setattr(ij, "RETRY_BASE_DELAY", 0.0)


def _card(
    *,
    job_id: str,
    title: str = "Engenheiro de Software",
    company: str | None = "Acme Tech",
    company_href: str | None = "https://www.infojobs.com.br/acme-tech",
    location: str = "São Paulo - SP",
    posted_at: str = "2026/05/12 12:03:00",
    salary: str = "A combinar",
    modality_icon: str = "buildings",
    modality_label: str = "Presencial",
    education: str = "Ensino Médio (2º Grau)",
    experience: str = "Sem experiência",
    description: str = "Vaga incrível em uma empresa em crescimento.",
) -> str:
    """Build a realistic card matching the live HTML structure."""
    if company is None:
        company_block = (
            '<div class="text-body">\n'
            '    Empresa\n'
            '    <span class="text-nowrap">confidencial</span>\n'
            '</div>'
        )
    else:
        company_block = (
            '<div class="text-body">\n'
            f'    <a class="text-body text-decoration-none" href="{company_href}">\n'
            f'        {company}\n'
            '    </a>\n'
            '</div>'
        )
    slug = re.sub(r"[^a-z0-9-]", "-", title.lower().replace(" ", "-"))
    href = f"/vaga-de-{slug}__{job_id}.aspx"
    return (
        f'<div data-typesimilar="" class="card card-shadow js_rowCard active">'
        f'<div id="vacancy{job_id}" data-modelversion="" data-id="{job_id}" '
        f'class="js_vacancyLoad js_cardLink" data-href="{href}" '
        f'data-testabbutton="false">'
        f'<div class="d-flex flex-wrap gap-8">'
        f'<div hidden class="js_date" data-value="{posted_at}">'
        f'<div class="tag mb-2 tag-outline-premium tag-sm"><span>NOVA</span></div>'
        f'</div></div>'
        f'<div class="d-flex gap-8 justify-content-between">'
        f'<a class="text-decoration-none" href="{href}">'
        f'<h2 class="h3 font-weight-bold text-body mb-2 js_vacancyTitle">{title}</h2>'
        f'</a>'
        f'<div class="text-medium small text-nowrap">Hoje</div>'
        f'</div>'
        f'<div class="d-flex align-items-baseline">{company_block}</div>'
        f'<div class="mb-8">{location}</div>'
        f'<div class="d-inline-flex flex-wrap mb-8 text-medium">'
        f'<div><svg class="icon icon-money   icon-size-16"><use xlink:href="#money" /></svg> {salary}</div>'
        f'<div><svg class="icon icon-suitcase   icon-size-16"><use xlink:href="#suitcase" /></svg> {experience}</div>'
        f'<div><svg class="icon icon-graduate-hat   icon-size-16"><use xlink:href="#graduate-hat" /></svg> {education}</div>'
        f'<div><svg class="icon icon-{modality_icon}   icon-size-16"><use xlink:href="#{modality_icon}" /></svg> {modality_label}</div>'
        f'</div>'
        f'<div class="text-medium">{description}</div>'
        f'</div></div>'
    )


def _fragment(cards: list[str], *, eof: bool = False) -> dict:
    """Wrap card HTML the way the live API does."""
    body = (
        '<div class="js_vacanciesGridFragment mb-16">'
        + "".join(cards)
        + '</div>'
    )
    return {"eof": eof, "listFragmentHTML": body}


def _empty_fragment(*, eof: bool = False) -> dict:
    return {"eof": eof, "listFragmentHTML": '<div class="js_vacanciesGridFragment mb-16"></div>'}


# --- registry ---------------------------------------------------------------


def test_registry_resolves_infojobs_br() -> None:
    assert ScraperRegistry.get(ATSType.INFOJOBSBR) is InfoJobsBrasilScraper


def test_ats_type_value_is_infojobs_br() -> None:
    """The enum value drives every downstream CSV column name and
    storage path — pin it explicitly."""
    assert ATSType.INFOJOBSBR.value == "infojobs_br"


# --- happy path -------------------------------------------------------------


def test_parses_full_card(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D1"),
        json=_fragment([_card(
            job_id="11608342",
            title="Promotor de Vendas",
            company="Gi Group",
            location="São Paulo - SP",
            posted_at="2026/05/12 12:03:00",
            salary="R$ 1.700,00 a R$ 2.000,00",
            modality_icon="buildings",
            modality_label="Presencial",
            description="Vaga de promotor para grande rede.",
        )], eof=True),
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=1).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.INFOJOBSBR
    assert j.ats_id == "11608342"
    assert j.title == "Promotor de Vendas"
    assert j.company == "Gi Group"
    assert j.location == "São Paulo - SP"
    assert j.country_iso == "BR"
    assert j.language == "pt"
    assert j.is_remote is False
    assert j.salary_currency == "BRL"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 1700.0
    assert j.salary_max == 2000.0
    assert j.salary_summary == "R$ 1.700,00 a R$ 2.000,00"
    assert j.description == "Vaga de promotor para grande rede."
    assert j.posted_at is not None
    assert j.posted_at.year == 2026 and j.posted_at.month == 5 and j.posted_at.day == 12
    assert str(j.url).endswith(
        "/vaga-de-promotor-de-vendas__11608342.aspx"
    )
    assert j.raw is not None
    assert j.raw["education"] == "Ensino Médio (2º Grau)"
    assert j.raw["modality"] == "Presencial"


def test_company_falls_back_to_empresa_confidencial(httpx_mock) -> None:
    """When there's no anchor to a company page the card displays
    'Empresa confidencial' and the scraper should propagate that
    verbatim instead of inventing a name."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D1"),
        json=_fragment([_card(job_id="1", company=None)], eof=True),
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=1).fetch()
    assert jobs[0].company == "Empresa confidencial"


def test_hibrido_modality_sets_is_remote_false(httpx_mock) -> None:
    """Híbrido (icon-house-and-building) → not fully remote."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D1"),
        json=_fragment([_card(
            job_id="1",
            modality_icon="house-and-building",
            modality_label="Híbrido",
        )], eof=True),
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=1).fetch()
    assert jobs[0].is_remote is False
    assert jobs[0].raw is not None
    assert jobs[0].raw["modality"] == "Híbrido"


def test_home_office_modality_sets_is_remote_true(httpx_mock) -> None:
    """Remote roles appear with Home Office / Remoto labels."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D1"),
        json=_fragment([_card(
            job_id="1",
            modality_icon="buildings",
            modality_label="Home Office",
            location="Remoto",
        )], eof=True),
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=1).fetch()
    assert jobs[0].is_remote is True


def test_salary_a_combinar_yields_no_signal(httpx_mock) -> None:
    """``A combinar`` (negotiable) is the most common label on
    InfoJobs cards — no numeric extraction should fire."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D1"),
        json=_fragment([_card(job_id="1", salary="A combinar")], eof=True),
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=1).fetch()
    j = jobs[0]
    assert j.salary_currency is None
    assert j.salary_min is None and j.salary_max is None
    assert j.salary_summary is None


# --- salary parsing ---------------------------------------------------------


def test_parse_brl_amount_handles_brazilian_thousand_separator() -> None:
    """Brazilian formatting uses ``.`` as thousands, ``,`` as decimal —
    opposite to en-US. Mishandling collapses ``R$ 1.700,00`` into
    1.7 instead of 1700.0."""
    assert _parse_brl_amount("1.700,00") == 1700.0
    assert _parse_brl_amount("8.000,00") == 8000.0
    assert _parse_brl_amount("99.000,00") == 99000.0
    assert _parse_brl_amount("3.500,50") == 3500.50
    assert _parse_brl_amount("0") is None
    assert _parse_brl_amount("") is None


@pytest.mark.parametrize("raw, expected", [
    ("A combinar", (None, None, None, None)),
    ("", (None, None, None, None)),
    (None, (None, None, None, None)),
    (
        "R$ 1.700,00 a R$ 2.000,00",
        (1700.0, 2000.0, "BRL", "R$ 1.700,00 a R$ 2.000,00"),
    ),
    (
        "Até R$ 5.000,00",
        (None, 5000.0, "BRL", "Até R$ 5.000,00"),
    ),
    (
        "A partir de R$ 8.000,00",
        (8000.0, None, "BRL", "A partir de R$ 8.000,00"),
    ),
    ("R$ 3.500,00", (3500.0, 3500.0, "BRL", "R$ 3.500,00")),
])
def test_parse_salary_shapes(raw, expected) -> None:
    assert _parse_salary(raw) == expected


def test_parse_salary_normalizes_whitespace_in_summary() -> None:
    """The real DOM ships values like ``R$ 1.700,00\\n  a\\n  R$ 2.000,00``
    with embedded line breaks. The summary should collapse those so
    consumers see a clean single-line string."""
    raw = "R$ 1.700,00\r\n                a\r\n                R$ 2.000,00"
    min_, max_, cur, summary = _parse_salary(raw)
    assert min_ == 1700.0 and max_ == 2000.0 and cur == "BRL"
    assert summary == "R$ 1.700,00 a R$ 2.000,00"


# --- date parsing ------------------------------------------------------------


def test_parse_brazilian_date_full_timestamp() -> None:
    """The hidden ``js_date`` block carries a structured timestamp —
    parse it directly, don't try to derive from 'Hoje' / 'Ontem'."""
    dt = _parse_brazilian_date("2026/05/12 12:03:00")
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 5, 12, 15)
    assert dt.tzinfo is not None


def test_parse_brazilian_date_dd_mm_yyyy() -> None:
    """Some legacy detail pages use DD/MM/YYYY only — also accept that
    form so we don't crash if the listing emits it."""
    dt = _parse_brazilian_date("12/05/2026")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 5, 12)


def test_parse_brazilian_date_invalid_returns_none() -> None:
    assert _parse_brazilian_date("") is None
    assert _parse_brazilian_date("never") is None


# --- remote inference --------------------------------------------------------


@pytest.mark.parametrize("modality, location, expected", [
    ("Home Office", "Brasil", True),
    ("Remoto", "São Paulo - SP", True),
    ("Presencial", "Belo Horizonte - MG", False),
    ("Híbrido", "Curitiba - PR", False),
    (None, "Home Office", True),
    (None, None, None),
])
def test_infer_remote(modality, location, expected) -> None:
    assert _infer_remote(modality, location) is expected


# --- URL helper --------------------------------------------------------------


def test_set_query_param_appends_when_missing() -> None:
    out = _set_query_param("https://example.com/x.aspx", "page", "2")
    assert out == "https://example.com/x.aspx?page=2"


def test_set_query_param_overwrites_existing() -> None:
    """``?page=1&foo=bar`` updated with page=3 should become
    ``?foo=bar&page=3`` — the foo param stays, the old page= is
    dropped, the new one appended. parse_qsl preserves order minus
    the removed key."""
    out = _set_query_param(
        "https://example.com/x.aspx?page=1&foo=bar", "page", "3",
    )
    parsed = urllib.parse.urlparse(out)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params == {"page": "3", "foo": "bar"}


# --- pagination termination --------------------------------------------------


def test_stops_when_eof_true(httpx_mock) -> None:
    """The fragment payload carries an ``eof`` boolean; honor it as
    the cheapest stop signal rather than walking to max_pages."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE),
        json=_fragment([_card(job_id="100"), _card(job_id="101")], eof=True),
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=50).fetch()
    assert {j.ats_id for j in jobs} == {"100", "101"}


def test_stops_after_three_consecutive_duplicate_pages(httpx_mock) -> None:
    """If ``eof`` stays False but the page returns 0 new ids three
    times in a row we stop. Real InfoJobs pagination occasionally
    re-emits the last page when you walk past the live tail."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D1"),
        json=_fragment([_card(job_id="100"), _card(job_id="101")]),
    )
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE),
        json=_fragment([_card(job_id="100"), _card(job_id="101")]),
        is_reusable=True,
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=20).fetch()
    assert {j.ats_id for j in jobs} == {"100", "101"}


def test_paginates_until_eof(httpx_mock) -> None:
    """Distinct ids per page → walk through until ``eof: True``."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D1"),
        json=_fragment([_card(job_id="1"), _card(job_id="2")]),
    )
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D2"),
        json=_fragment([_card(job_id="3"), _card(job_id="4")]),
    )
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE.pattern + r".*page%3D3"),
        json=_fragment([_card(job_id="5")], eof=True),
    )
    jobs = InfoJobsBrasilScraper("any", max_pages=10).fetch()
    assert {j.ats_id for j in jobs} == {"1", "2", "3", "4", "5"}


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even if every page is fresh and ``eof`` never fires, the cap
    is hard. Prevents a buggy site from looping forever."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE),
        json=_fragment([_card(job_id="42")]),  # same id every page
        is_reusable=True,
    )
    # Three consecutive dupes would stop us at page 4; the cap should
    # bite before that. Set max_pages=2 and check we got exactly 1 job
    # (deduped across pages 1+2).
    jobs = InfoJobsBrasilScraper("any", max_pages=2).fetch()
    assert {j.ats_id for j in jobs} == {"42"}


# --- error paths ------------------------------------------------------------


def test_non_json_response_raises_scraper_error(httpx_mock) -> None:
    """The API endpoint sometimes returns 200 with an HTML interstitial
    when CDN caching misbehaves — surface that as a typed ScraperError."""
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE),
        text="<html>error page</html>",
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="non-JSON"):
        InfoJobsBrasilScraper("any", max_pages=1).fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE),
        status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        InfoJobsBrasilScraper("any", max_pages=1).fetch()


# --- listing-URL passthrough -------------------------------------------------


def test_fragment_url_carries_listing_with_page_param(httpx_mock) -> None:
    """The backend reads ``page`` from the encoded listing URL, not
    from the request URL. We verify the captured request URL contains
    the listing URL with ``page=1`` percent-encoded inside.

    Also pins the listing-URL override knob — a per-city listing URL
    passes through verbatim with only ``page=N`` appended.
    """
    httpx_mock.add_response(
        url=re.compile(_FRAGMENT_RE),
        json={"eof": True, "listFragmentHTML": ""},
        is_reusable=True,
    )
    InfoJobsBrasilScraper(
        "any", max_pages=1,
        listing_url="https://www.infojobs.com.br/empregos-em-rio-de-janeiro.aspx",
    ).fetch()
    requests = httpx_mock.get_requests()
    assert requests, "expected at least one fragment request"
    qs = urllib.parse.urlparse(str(requests[0].url)).query
    inner = urllib.parse.parse_qs(qs)["url"][0]
    inner_parsed = urllib.parse.urlparse(inner)
    inner_params = urllib.parse.parse_qs(inner_parsed.query)
    assert inner_parsed.path == "/empregos-em-rio-de-janeiro.aspx"
    assert inner_params["page"] == ["1"]
