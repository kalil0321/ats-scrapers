"""Tests for the Manfred (Spanish-speaking dev jobs) scraper.

Pin parsing of Manfred's rich payload (salaryFrom/To, currency
symbol → ISO mapping, remotePercentage threshold, status filter)
plus the lang validation contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ManfredScraper, ScraperRegistry


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.manfred as m
    monkeypatch.setattr(m, "MAX_RETRIES", 1)
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


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
