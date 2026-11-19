"""Tests for the Manfred (Spanish-speaking dev jobs) scraper.

Pin parsing of Manfred's rich payload (salaryFrom/To, currency
symbol → ISO mapping, remotePercentage threshold, status filter)
plus the lang validation contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ManfredScraper, ScraperRegistry


def _offer(
    *,
    slug: str = "acme-data-scientist",
    position: str = "Data Scientist",
    company: str = "Acme",
    locations: list[str] | None = None,
    salary_from: int = 50000,
    salary_to: int = 75000,
    currency: str = "€",
    remote_pct: int = 50,
    status: str = "ACTIVE",
    bonus: int = 0,
    equity_inf: int = 0,
    equity_sup: int = 0,
    internal_code: str = "AC-001",
    offer_id: int = 8355,
) -> dict[str, Any]:
    return {
        "id": offer_id,
        "slug": slug,
        "position": position,
        "status": status,
        "internalCode": internal_code,
        "salaryFrom": salary_from,
        "salaryTo": salary_to,
        "bonus": bonus,
        "remotePercentage": remote_pct,
        "equityInf": equity_inf,
        "equitySup": equity_sup,
        "currency": currency,
        "noStack": False,
        "highlights": [],
        # ``None`` → default to Madrid; empty list → keep empty so tests
        # can exercise the no-location fallback path explicitly.
        "locations": ["Madrid, Spain"] if locations is None else locations,
        "offerLanguages": ["ES"],
        "updatedAt": "2026-05-07T07:53:59.184Z",
        "company": {"name": company, "web": f"https://{company.lower()}.com"},
    }


def _api_url(lang: str = "EN") -> str:
    return f"https://www.getmanfred.com/api/v2/public/offers?lang={lang}"


def _detail_url(offer_id: int = 8355, lang: str = "EN") -> str:
    return f"https://www.getmanfred.com/api/v2/public/offers/{offer_id}?lang={lang}"


def _detail() -> dict[str, Any]:
    return {
        "introduction": "Build **products**.",
        "responsibilities": ["Own APIs", "Improve reliability"],
        "whatOffering": "Remote setup.",
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_manfred() -> None:
    assert ScraperRegistry.get(ATSType.MANFRED) is ManfredScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_offer(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json=[_offer()])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    j = ManfredScraper("any").fetch()[0]
    assert j.source_updated_at == datetime(2026, 5, 7, 7, 53, 59, 184000, tzinfo=UTC)
    assert j.source_created_at is None
    assert j.ats_type is ATSType.MANFRED
    assert j.ats_id == "acme-data-scientist"
    assert j.title == "Data Scientist"
    assert j.company == "Acme"
    assert j.location == "Madrid, Spain"
    assert j.is_remote is True  # 50% threshold
    assert j.salary_currency == "EUR"  # € → EUR
    assert j.salary_min == 50000
    assert j.salary_max == 75000
    assert j.requisition_id == "AC-001"
    assert j.description == "Build products.\n\n- Own APIs\n- Improve reliability\n\nRemote setup."
    assert j.posted_at is not None
    assert str(j.url) == "https://www.getmanfred.com/job-offers/acme-data-scientist"


# --- status filter ----------------------------------------------------------


def test_drops_non_active_offers(httpx_mock) -> None:
    """``status='CLOSED'`` / 'DRAFT' rows must not surface — Manfred
    keeps closed roles in the API response with the same shape."""
    httpx_mock.add_response(url=_api_url(), json=[
        _offer(slug="active", status="ACTIVE"),
        _offer(slug="closed", status="CLOSED"),
        _offer(slug="draft", status="DRAFT"),
    ])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    jobs = ManfredScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["active"]


# --- remote-percentage threshold --------------------------------------------


@pytest.mark.parametrize("pct, expected", [
    (0, False),
    (49, False),
    (50, True),  # threshold
    (80, True),
    (100, True),
])
def test_remote_percentage_threshold(pct: int, expected: bool, httpx_mock) -> None:
    """Manfred's ``remotePercentage`` is a 0..100 weekly-remote share.
    ``>= 50`` is the line for is_remote=True."""
    httpx_mock.add_response(url=_api_url(), json=[_offer(slug=f"p{pct}", remote_pct=pct)])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    assert ManfredScraper("any").fetch()[0].is_remote is expected


# --- currency mapping -------------------------------------------------------


@pytest.mark.parametrize("symbol, expected", [
    ("€", "EUR"),
    ("$", "USD"),
    ("£", "GBP"),
])
def test_currency_symbol_to_iso(symbol: str, expected: str, httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json=[_offer(slug=f"c-{symbol}", currency=symbol)])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    assert ManfredScraper("any").fetch()[0].salary_currency == expected


def test_no_salary_currency_when_amounts_zero(httpx_mock) -> None:
    """If both salaryFrom and salaryTo are 0/missing, salary_currency
    stays None (don't invent a EUR for empty rows)."""
    httpx_mock.add_response(url=_api_url(), json=[
        _offer(slug="no-sal", salary_from=0, salary_to=0)
    ])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    j = ManfredScraper("any").fetch()[0]
    assert j.salary_currency is None
    assert j.salary_min is None
    assert j.salary_max is None


# --- locations --------------------------------------------------------------


def test_multiple_locations_pipe_joined(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json=[_offer(
        slug="multi", locations=["Madrid, Spain", "Barcelona, Spain"],
    )])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    assert ManfredScraper("any").fetch()[0].location == "Madrid, Spain | Barcelona, Spain"


def test_empty_locations_yields_none(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json=[_offer(slug="x", locations=[])])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    assert ManfredScraper("any").fetch()[0].location is None


# --- equity / bonus go into raw --------------------------------------------


def test_equity_and_bonus_stashed_in_raw(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json=[_offer(
        slug="eq", equity_inf=1000, equity_sup=5000, bonus=3000,
    )])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    j = ManfredScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw["equity_min"] == 1000
    assert j.raw["equity_max"] == 5000
    assert j.raw["bonus"] == 3000


# --- defensive --------------------------------------------------------------


def test_lang_validated_at_construction() -> None:
    with pytest.raises(ScraperError, match="lang"):
        ManfredScraper("any", lang="FR")


def test_es_lang_passes_through(httpx_mock) -> None:
    """``lang='ES'`` should hit the Spanish endpoint."""
    httpx_mock.add_response(url=_api_url("ES"), json=[_offer()])
    httpx_mock.add_response(url=_detail_url(lang="ES"), json=_detail())
    jobs = ManfredScraper("any", lang="ES").fetch()
    assert len(jobs) == 1


def test_skips_offer_missing_slug_or_position(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json=[
        _offer(slug="ok"),
        {"slug": "no-pos", "status": "ACTIVE", "company": {"name": "X"}},
        {"position": "no-slug", "status": "ACTIVE", "company": {"name": "X"}},
    ])
    httpx_mock.add_response(url=_detail_url(), json=_detail())
    jobs = ManfredScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["ok"]


def test_non_list_response_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json={"offers": []})
    with pytest.raises(ScraperError, match="API shape changed"):
        ManfredScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        ManfredScraper("any").fetch()


def test_visible_detail_sections_and_technical_punctuation(httpx_mock) -> None:
    detail = {
        **_detail(), "whatTheyAskFor": "Know **C#** and read pg_stat.",
        "inOneMonth": "Onboard.", "inThreeMonths": "Ship.", "inSixMonths": "Lead.",
        "faq": [{"question": "Where?", "answer": "Spain only."}],
        "watches": {"description": "Paid weekend shifts."},
        "perks": [{"name": "Health insurance"}],
    }
    httpx_mock.add_response(url=_api_url(), json=[_offer()])
    httpx_mock.add_response(url=_detail_url(), json=detail)
    [job] = ManfredScraper("any").fetch()
    for section in ("Know C# and read pg_stat.", "Onboard.", "Ship.", "Lead.",
                    "Where?", "Spain only.", "Paid weekend shifts.",
                    "Health insurance"):
        assert section in job.description


@pytest.mark.parametrize("defer", [False, True])
def test_tech_and_soft_skill_sections_survive_detail(httpx_mock, defer) -> None:
    httpx_mock.add_response(url=_api_url(), json=[_offer()])
    httpx_mock.add_response(url=_detail_url(), json={
        "techs": [
            {"name": "C#", "section": "MUST", "level": "BASIC"},
            {"name": "SQL", "section": "COULD", "level": "ADVANCED"},
            {"name": "Rust", "section": "EXTRA"}, {"name": "HTTP", "section": "NEW"},
            {"name": " C# ", "section": "MUST"}, {"level": "ADVANCED"},
        ],
        "softSkills": [{"name": "Teamwork", "section": "COULD"}, {"name": "Mentoring"}],
        "languages": [{"name": "English"}], "introduction": "Build products.",
    })
    scraper = ManfredScraper("any", include_descriptions=not defer)
    job, = scraper.fetch()
    if defer:
        assert job.skill_requirements is None
    assert scraper.get_description(job) == "Build products."
    assert [(s.label, s.requirement) for s in job.skill_requirements] == [
        ("C#", "required"), ("SQL", "preferred"), ("Rust", "optional"),
        ("HTTP", "unspecified"), ("Teamwork", "preferred"), ("Mentoring", "unspecified"),
    ]
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.parametrize("listing_clock", [None, "2026-05-09T15:00:00Z"])
def test_detail_update_fills_missing_listing_clock(listing_clock, httpx_mock) -> None:
    scraper = ManfredScraper("any", include_descriptions=False)
    job = scraper._parse({**_offer(), "updatedAt": listing_clock})
    assert job is not None
    httpx_mock.add_response(url=_detail_url(), json={
        "introduction": "Build products.", "updatedAt": "2026-05-07T15:00:00Z",
        "lastStatusChange": "2026-05-06T15:00:00Z",
    })
    assert scraper.get_description(job) == "Build products."
    day = 9 if listing_clock else 7
    assert job.source_updated_at == datetime(2026, 5, day, 15, tzinfo=UTC)


def test_listing_components_use_displayed_variable_and_equity_units(httpx_mock) -> None:
    httpx_mock.add_response(url=_api_url(), json=[
        _offer(slug="salary-variable", salary_from=45000, salary_to=50000, bonus=5000),
        _offer(slug="equity", equity_inf=0, equity_sup=1),
        _offer(slug="no-published-pay", salary_from=0, salary_to=0),
    ])
    paid, ownership, undisclosed = ManfredScraper("any", include_descriptions=False).fetch()
    base, variable = paid.compensation_components
    assert (base.minimum, base.maximum, base.currency, base.period) == (45000, 50000, "EUR", None)
    assert (variable.kind, variable.unit, variable.source_label, variable.currency) == (
        "bonus", "money", "Variable", "EUR",
    )
    assert variable.minimum == variable.maximum == 5000
    assert paid.salary_min == 45000 and paid.salary_max == 50000
    _, equity = ownership.compensation_components
    assert (equity.kind, equity.unit, equity.minimum, equity.maximum) == ("equity", "percent", None, 1)
    assert equity.currency is None and equity.period is None
    assert undisclosed.compensation_components is None
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize(("currency", "bonus", "unit", "expected_currency"), [
    (None, 10, None, None),
    ("€", "10%", "percent", None),
    ("€", 10, "money", "EUR"),
])
def test_variable_pay_units_require_evidence(currency, bonus, unit, expected_currency):
    job = ManfredScraper("any")._parse({**_offer(), "bonus": bonus, "currency": currency})
    base, variable = job.compensation_components
    assert (variable.unit, variable.currency, variable.minimum) == (unit, expected_currency, 10)
    assert variable.period is None
    assert base.kind == "base_salary"
    assert job.salary_period == "YEAR"
