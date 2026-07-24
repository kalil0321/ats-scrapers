"""Tests for the elempleo (Colombia) scraper.

The site is server-rendered HTML — these tests stub the listing
page responses with realistic card markup and assert that every
field maps to the right ``Job`` slot. The fixtures mirror the
selectors documented in ``elempleo.py``:

  - ``js-offer-title`` + ``title="…"``
  - ``js-offer-company`` / ``js-offer-city``
  - ``data-offer-id`` (canonical id; trailing slug id is the fallback)
  - labelled-pair triplet for Salario / Tipo de contrato / Modalidad

Pagination terminates on the first empty page, and the Colombian
salary format (``$1,5 a $2 millones`` → 1.5M–2M COP) needs the
comma-as-decimal handling exercised explicitly.
"""

from __future__ import annotations

import re

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ElempleoScraper, ScraperRegistry
from ats_scrapers.scrapers.elempleo import (
    _EMPLOYMENT_MAP,
    _parse_co_number,
    _parse_salary,
    _strip_accents,
)

_LISTING_RE = re.compile(r"^https://www\.elempleo\.com/co/ofertas-empleo\?Page=\d+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop retry backoff so failure-path tests don't take 30s each."""
    import ats_scrapers.scrapers.elempleo as m
    monkeypatch.setattr(m, "MAX_RETRIES", 1)
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


def _card(
    *,
    offer_id: str,
    title: str,
    company: str = "Acme SAS",
    city: str = "Bogotá",
    salary: str = "$2 a $2,5 millones",
    contract: str = "Indefinido",
    modality: str = "Presencial",
    date_label: str = "Hoy",
    description: str = "",
    omit_data_offer_id: bool = False,
) -> str:
    """Realistic single-card fragment.

    Mirrors the structure live elempleo ships — wrapper div carrying
    ``col-md-12 result-item mb-3 bg-white``, the title anchor with
    ``title="…"`` and ``js-offer-title`` class, the company span with
    ``js-offer-company``, the labelled triplet for salary/contract/
    modality, and the share button carrying ``data-offer-id`` (the
    canonical id source) plus an optional ``data-offer-description``.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", title.lower().replace(" ", "-"))
    href = f"/co/ofertas-trabajo/{slug}-{offer_id}"
    data_offer_id = "" if omit_data_offer_id else f'data-offer-id="{offer_id}"'
    desc_attr = (
        f'data-offer-description="{description}"' if description else ""
    )
    return (
        f'<div class="col-md-12 result-item mb-3 bg-white" '
        f'style="text-align: left;">'
        # title
        f'<a class="text-ellipsis js-offer-title fw-bold" '
        f'href="{href}" title="{title}">{title}</a>'
        # company
        f'<span class="info-company-name js-offer-company fs-6">'
        f'{company}</span>'
        # salary
        f'<div class="text-blue-petrol-dark">{salary}</div>'
        f'<div class="small-text pb-2 text-medium-gray">Salario</div>'
        # contract
        f'<div class="text-blue-petrol-dark">{contract}</div>'
        f'<div class="small-text pb-2 text-medium-gray">Tipo de contrato</div>'
        # city
        f'<span class="info-city js-offer-city text-blue-petrol-dark">'
        f'{city}</span>'
        f'<div class="small-text pb-2 text-medium-gray">Ubicación</div>'
        # modality
        f'<div class="text-blue-petrol-dark">{modality}</div>'
        f'<div class="small-text pb-2 text-medium-gray">Modalidad laboral</div>'
        # date
        f'<span class="rounded-pill js-offer-date mi-etiqueta">'
        f'<i class="fa fa-clock-o"></i>{date_label}</span>'
        # share button (carries the canonical id + description)
        f'<a {data_offer_id} {desc_attr} href="#">Compartir</a>'
        f'</div>'
    )


def _empty_listing() -> str:
    return "<html><body><div class='no-results'>0</div></body></html>"


def _listing(cards: list[str]) -> str:
    # Trailing pagination sentinel so the card regex knows where to stop.
    pagination = '<div class="text-center pt-3">pagination</div>'
    return f"<html><body>{''.join(cards)}{pagination}</body></html>"


# --- registry ---------------------------------------------------------------


def test_registry_resolves_elempleo() -> None:
    assert ScraperRegistry.get(ATSType.ELEMPLEO) is ElempleoScraper


def test_ats_type_value() -> None:
    assert ATSType.ELEMPLEO.value == "elempleo"


# --- happy path -------------------------------------------------------------


def test_parses_full_listing_card(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(
            offer_id="1886709556",
            title="Terapeuta ocupacional domiciliario",
            company="Health &amp; Life IPS SAS",
            city="Bucaramanga",
            salary="$1,5 a $2 millones",
            contract="Prestacion de Servicios",
            modality="Presencial",
            date_label="Hoy",
            description="Vacante de terapia ocupacional...",
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.elempleo\.com/co/ofertas-empleo\?Page=[2-9]$"),
        text=_listing([]),
        is_reusable=True,
    )

    jobs = ElempleoScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.ELEMPLEO
    assert j.ats_id == "1886709556"
    assert j.global_id == "elempleo:1886709556"
    assert j.title == "Terapeuta ocupacional domiciliario"
    # HTML entity must be decoded — "&amp;" → "&".
    assert j.company == "Health & Life IPS SAS"
    assert j.location == "Bucaramanga, Colombia"
    assert j.country_iso == "CO"
    assert j.region == "South America"
    assert j.language == "es"
    assert j.is_remote is False  # Presencial
    assert j.salary_currency == "COP"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 1_500_000
    assert j.salary_max == 2_000_000
    assert j.salary_summary == "$1,5 a $2 millones"
    # Prestacion de Servicios → CONTRACT (independent contractor).
    assert j.employment_type == "CONTRACT"
    assert j.commitment == "Prestacion de Servicios"
    assert j.description == "Vacante de terapia ocupacional..."
    assert j.posted_at is not None  # "Hoy" parses to today
    assert str(j.url) == (
        "https://www.elempleo.com/co/ofertas-trabajo/"
        "terapeuta-ocupacional-domiciliario-1886709556"
    )
    assert j.raw is not None
    assert j.raw["salary_text"] == "$1,5 a $2 millones"
    assert j.raw["modality"] == "Presencial"
    assert j.raw["contract_type"] == "Prestacion de Servicios"


def test_card_class_with_live_trailing_space_parses(httpx_mock) -> None:
    card = _card(offer_id="123", title="Trailing Space").replace(
        'bg-white"', 'bg-white "', 1,
    )
    httpx_mock.add_response(url=_LISTING_RE, text=_listing([card]))
    jobs = ElempleoScraper("any", max_pages=1).fetch()
    assert [job.ats_id for job in jobs] == ["123"]


def test_remote_modality_sets_is_remote(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(
            offer_id="1", title="Ejecutivo Remoto", modality="Remoto",
        )]),
    )
    httpx_mock.add_response(
        url=_LISTING_RE, text=_listing([]), is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    assert jobs[0].is_remote is True


def test_hybrid_modality_is_not_remote(httpx_mock) -> None:
    """Híbrido is on-site sometimes — we conservatively flag it as
    not-fully-remote so the public dataset's is_remote filter stays
    honest."""
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(
            offer_id="1", title="Híbrido role", modality="Híbrido",
        )]),
    )
    httpx_mock.add_response(
        url=_LISTING_RE, text=_listing([]), is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    assert jobs[0].is_remote is False


def test_falls_back_to_slug_id_when_data_offer_id_missing(httpx_mock) -> None:
    """The share-button anchor is occasionally rendered without
    ``data-offer-id`` (some legacy / preview variants). The trailing
    numeric slug chunk is the documented fallback."""
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(
            offer_id="9876543", title="Test", omit_data_offer_id=True,
        )]),
    )
    httpx_mock.add_response(
        url=_LISTING_RE, text=_listing([]), is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    assert jobs[0].ats_id == "9876543"


def test_confidential_salary_yields_no_numeric_signal(httpx_mock) -> None:
    """``Salario confidencial`` is the most common salary value
    (~7,300 of the ~10k live postings). Treat it as no-signal — keep
    summary None so the parquet schema isn't polluted with the
    placeholder string, currency stays None."""
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(
            offer_id="1", title="X", salary="Salario confidencial",
        )]),
    )
    httpx_mock.add_response(
        url=_LISTING_RE, text=_listing([]), is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    j = jobs[0]
    assert j.salary_currency is None
    assert j.salary_min is None
    assert j.salary_max is None
    # We still record the raw text for debugging / downstream parsing.
    assert j.raw is not None and j.raw["salary_text"] == "Salario confidencial"


# --- salary parsing --------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("$1,5 a $2 millones",   (1_500_000, 2_000_000, "COP", "MONTH")),
    ("$2 a $2,5 millones",   (2_000_000, 2_500_000, "COP", "MONTH")),
    ("$12,5 a $15 millones", (12_500_000, 15_000_000, "COP", "MONTH")),
    ("$10 millones",         (10_000_000, 10_000_000, "COP", "MONTH")),
    ("Salario confidencial", (None, None, None, None)),
    ("",                     (None, None, None, None)),
    (None,                   (None, None, None, None)),
])
def test_parse_salary_shapes(raw, expected) -> None:
    assert _parse_salary(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("1,5", 1.5),
    ("12,5", 12.5),
    ("2", 2.0),
    ("1.000,5", 1000.5),
    ("0", None),
    ("", None),
])
def test_parse_co_number_decimal_comma(raw, expected) -> None:
    assert _parse_co_number(raw) == expected


# --- contract type mapping --------------------------------------------------


@pytest.mark.parametrize("contract, expected", [
    ("Indefinido",              "FULL_TIME"),
    ("Definido",                "TEMPORARY"),
    ("Por obra o labor",        "CONTRACT"),
    ("Prestacion de Servicios", "CONTRACT"),
    ("Contrato de aprendizaje", "INTERN"),
])
def test_contract_type_maps_to_employment_type(
    contract: str, expected: str, httpx_mock
) -> None:
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(offer_id="1", title="X", contract=contract)]),
    )
    httpx_mock.add_response(
        url=_LISTING_RE, text=_listing([]), is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    assert jobs[0].employment_type == expected
    assert jobs[0].commitment == contract


def test_employment_map_keys_are_accent_free() -> None:
    """The contract-label lookup ASCII-folds before matching so the
    same map handles ``Híbrido`` and ``Hibrido``. Guard against an
    accidental accented key sneaking in — it would silently miss."""
    for key in _EMPLOYMENT_MAP:
        assert _strip_accents(key) == key, key


# --- placeholder handling ---------------------------------------------------


def test_mustache_placeholders_are_ignored(httpx_mock) -> None:
    """elempleo renders dates / contract for older postings as
    Mustache placeholders like ``{{contracttypetxt}}`` that hydrate
    client-side. We treat any value starting with ``{{`` as missing
    so the dataset isn't polluted with literal placeholder strings."""
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(
            offer_id="1", title="X",
            contract="{{contracttypetxt}}", salary="{{salaryInfo}}",
            date_label="{{publishDateInfo}}",
        )]),
    )
    httpx_mock.add_response(
        url=_LISTING_RE, text=_listing([]), is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    j = jobs[0]
    assert j.employment_type is None
    assert j.commitment is None
    assert j.salary_summary is None
    assert j.salary_currency is None
    assert j.posted_at is None  # not "Hoy" → don't synthesize a date


# --- pagination -------------------------------------------------------------


def test_paginates_multiple_pages(httpx_mock) -> None:
    """Each page surfaces 20 unique cards; the scraper should
    accumulate across pages until it hits two consecutive empties."""
    page1 = _listing([
        _card(offer_id=f"{100+i}", title=f"Job {i}") for i in range(3)
    ])
    page2 = _listing([
        _card(offer_id=f"{200+i}", title=f"Job p2 {i}") for i in range(2)
    ])
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1", text=page1,
    )
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=2", text=page2,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.elempleo\.com/co/ofertas-empleo\?Page=[3-9]$"),
        text=_listing([]),
        is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {
        "100", "101", "102", "200", "201",
    }


def test_stops_after_two_consecutive_empty_pages(httpx_mock) -> None:
    """Pages beyond the live tail return HTTP 200 with zero cards.
    The scraper tolerates one empty page (mid-stream render glitch)
    but stops at two consecutive empties — page 4 must never be
    requested."""
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([_card(offer_id="1", title="A")]),
    )
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=2",
        text=_listing([]),
    )
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=3",
        text=_listing([]),
    )
    # Page 4 has no stub — httpx_mock would error if it were requested.
    jobs = ElempleoScraper("any", max_pages=20).fetch()
    assert len(jobs) == 1


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Hard ceiling so a buggy site returning fresh-looking cards
    forever can't run unbounded."""
    for p in range(1, 4):
        httpx_mock.add_response(
            url=f"https://www.elempleo.com/co/ofertas-empleo?Page={p}",
            text=_listing([_card(offer_id=str(p * 10), title=f"Job {p}")]),
        )
    jobs = ElempleoScraper("any", max_pages=3).fetch()
    assert len(jobs) == 3


def test_deduplicates_repeated_ids_across_pages(httpx_mock) -> None:
    """When the same posting appears on two pages (rare — happens
    when a fresh job pushes a stale one across a page boundary mid-
    scrape), de-dup by ats_id."""
    card = _card(offer_id="555", title="Dup")
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=1",
        text=_listing([card]),
    )
    httpx_mock.add_response(
        url="https://www.elempleo.com/co/ofertas-empleo?Page=2",
        text=_listing([card]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.elempleo\.com/co/ofertas-empleo\?Page=[3-9]$"),
        text=_listing([]),
        is_reusable=True,
    )
    jobs = ElempleoScraper("any").fetch()
    assert len(jobs) == 1


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE, status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        ElempleoScraper("any").fetch()


def test_unexpected_status_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE, status_code=403, is_reusable=True,
    )
    with pytest.raises(ScraperError, match="403"):
        ElempleoScraper("any").fetch()
