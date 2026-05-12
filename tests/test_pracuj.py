"""Tests for the Pracuj.pl scraper.

Pin the __NEXT_DATA__ extraction, the Polish contract-type → EmploymentType
mapping, multi-location offer fan-out, salary currency detection, and the
``?pn=N`` pagination contract.
"""

from __future__ import annotations

import json
import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import PracujScraper, ScraperRegistry

_LISTING_RE = re.compile(r"^https://www\.pracuj\.pl/praca\?pn=\d+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.pracuj as pj
    monkeypatch.setattr(pj, "MAX_RETRIES", 1)
    monkeypatch.setattr(pj, "RETRY_BASE_DELAY", 0.0)


def _next_data_html(groups: list[dict]) -> str:
    """Build a pracuj.pl listing HTML page with __NEXT_DATA__ embedded the
    same way the live Next.js app does."""
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "jobOffers": {
                        "groupedOffers": groups,
                        "offersTotalCount": len(groups),
                        "groupedOffersTotalCount": len(groups),
                    },
                    "dictionaries": {},
                },
            },
        },
        "page": "/praca",
        "query": {},
    }
    body = json.dumps(payload, ensure_ascii=False)
    return (
        '<!DOCTYPE html><html lang="pl"><body>'
        '<script id="__NEXT_DATA__" type="application/json">'
        f'{body}'
        '</script></body></html>'
    )


def _empty_page() -> str:
    return _next_data_html([])


def _group(
    *,
    group_id: str = "g-1",
    title: str = "Senior Java Developer",
    company: str = "Acme Sp. z o.o.",
    offers: list[dict] | None = None,
    salary: str = "",
    contracts: list[str] | None = None,
    work_modes: list[str] | None = None,
    work_schedules: list[str] | None = None,
    position_levels: list[str] | None = None,
    last_publicated: str = "2026-05-01T09:00:00.000Z",
    description: str = "Zakres obowiązków: pisanie kodu.",
) -> dict:
    return {
        "groupId": group_id,
        "jobTitle": title,
        "companyName": company,
        "companyId": 12345,
        "lastPublicated": last_publicated,
        "salaryDisplayText": salary,
        "jobDescription": description,
        "offers": offers if offers is not None else [
            {
                "partitionId": 1001,
                "offerAbsoluteUri": "https://www.pracuj.pl/praca/senior-java-warszawa,oferta,1001",
                "displayWorkplace": "Warszawa",
                "isWholePoland": False,
            },
        ],
        "positionLevels": (
            position_levels if position_levels is not None
            else ["Starszy specjalista (Senior)"]
        ),
        "typesOfContract": (
            contracts if contracts is not None else ["Umowa o pracę"]
        ),
        "workSchedules": (
            work_schedules if work_schedules is not None else ["Pełny etat"]
        ),
        "workModes": (
            work_modes if work_modes is not None else ["Praca hybrydowa"]
        ),
        "primaryAttributes": [],
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_pracuj() -> None:
    assert ScraperRegistry.get(ATSType.PRACUJ) is PracujScraper


# --- happy path -------------------------------------------------------------


def test_parses_minimal_listing(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group()]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )

    jobs = PracujScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.PRACUJ
    assert j.ats_id == "1001"
    assert j.global_id == "pracuj:1001"
    assert j.title == "Senior Java Developer"
    assert j.company == "Acme Sp. z o.o."
    assert j.country_iso == "PL"
    assert j.language == "pl"
    assert j.location == "Warszawa"
    assert j.is_remote is True  # 'Praca hybrydowa' counts as remote-capable
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Umowa o pracę"
    assert str(j.url).startswith(
        "https://www.pracuj.pl/praca/senior-java-warszawa"
    )
    assert j.description == "Zakres obowiązków: pisanie kodu."


def test_multi_location_offer_fans_out_into_n_jobs(httpx_mock) -> None:
    """A grouped offer with three ``offers[]`` entries (one per city)
    must expand into three Jobs with distinct ats_ids — Pracuj.pl's
    multi-region postings are not duplicates, they're discrete listings."""
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(
            title="System Engineer",
            offers=[
                {
                    "partitionId": 2001,
                    "offerAbsoluteUri": "https://www.pracuj.pl/praca/system-eng-kat,oferta,2001",
                    "displayWorkplace": "Katowice",
                    "isWholePoland": False,
                },
                {
                    "partitionId": 2002,
                    "offerAbsoluteUri": "https://www.pracuj.pl/praca/system-eng-kra,oferta,2002",
                    "displayWorkplace": "Kraków",
                    "isWholePoland": False,
                },
                {
                    "partitionId": 2003,
                    "offerAbsoluteUri": "https://www.pracuj.pl/praca/system-eng-rze,oferta,2003",
                    "displayWorkplace": "Rzeszów",
                    "isWholePoland": False,
                },
            ],
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = PracujScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"2001", "2002", "2003"}
    assert {j.location for j in jobs} == {"Katowice", "Kraków", "Rzeszów"}
    # All three should share title / company / employment_type
    assert {j.title for j in jobs} == {"System Engineer"}


def test_whole_poland_offer_marks_location(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(
            offers=[
                {
                    "partitionId": 3001,
                    "offerAbsoluteUri": "https://www.pracuj.pl/praca/x,oferta,3001",
                    "displayWorkplace": "Warszawa",
                    "isWholePoland": True,
                },
            ],
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = PracujScraper("any").fetch()[0]
    assert "Poland" in (j.location or "")
    assert j.raw is not None and j.raw.get("is_whole_poland") is True


# --- contract-type mapping --------------------------------------------------


@pytest.mark.parametrize(
    "contracts,expected",
    [
        (["Umowa o pracę"], "FULL_TIME"),
        (["Kontrakt B2B"], "CONTRACT"),
        (["Umowa zlecenie"], "CONTRACT"),
        (["Staż"], "INTERN"),
        (["Umowa na zastępstwo"], "TEMPORARY"),
        # Multi-type: full-time wins over the others by priority.
        (["Kontrakt B2B", "Umowa o pracę", "Umowa zlecenie"], "FULL_TIME"),
        # Unknown / empty stays None rather than guessing.
        ([], None),
        (["Strange brand-new label"], None),
    ],
)
def test_employment_type_mapping(httpx_mock, contracts, expected) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(contracts=contracts)]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = PracujScraper("any").fetch()[0]
    assert j.employment_type == expected


# --- work-mode → is_remote --------------------------------------------------


@pytest.mark.parametrize(
    "modes,expected",
    [
        (["Praca zdalna"], True),
        (["Praca hybrydowa"], True),
        (["Praca stacjonarna"], False),
        (["Praca mobilna"], False),
        (["Praca stacjonarna", "Praca zdalna"], True),
        ([], None),  # not classified → None (LLM enrichment fills it)
    ],
)
def test_is_remote_inference(httpx_mock, modes, expected) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(work_modes=modes)]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = PracujScraper("any").fetch()[0]
    assert j.is_remote is expected


# --- salary detection -------------------------------------------------------


def test_salary_with_zl_sets_pln_currency(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(
            salary="8 000–21 000 zł brutto / mies.",
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = PracujScraper("any").fetch()[0]
    assert j.salary_summary == "8 000–21 000 zł brutto / mies."
    assert j.salary_currency == "PLN"
    assert j.salary_period == "MONTH"


def test_empty_salary_text_leaves_currency_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(salary="")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = PracujScraper("any").fetch()[0]
    assert j.salary_summary is None
    assert j.salary_currency is None
    assert j.salary_period is None


def test_salary_with_dollar_sets_usd(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(
            salary="$3 000 - $5 000 net / month",
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = PracujScraper("any").fetch()[0]
    assert j.salary_currency == "USD"


# --- raw overflow -----------------------------------------------------------


def test_raw_captures_position_levels_and_work_schedules(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(
            position_levels=["Starszy specjalista (Senior)", "Ekspert"],
            work_modes=["Praca hybrydowa"],
            work_schedules=["Pełny etat"],
            contracts=["Umowa o pracę"],
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = PracujScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw["position_levels"] == ["Starszy specjalista (Senior)", "Ekspert"]
    assert j.raw["work_modes"] == ["Praca hybrydowa"]
    assert j.raw["work_schedules"] == ["Pełny etat"]
    assert j.raw["types_of_contract"] == ["Umowa o pracę"]
    assert j.raw["company_id"] == 12345


# --- skipping malformed entries ---------------------------------------------


def test_skips_offers_without_partition_id_or_url(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(
            offers=[
                {
                    "partitionId": 4001,
                    "offerAbsoluteUri": "https://www.pracuj.pl/praca/ok,oferta,4001",
                    "displayWorkplace": "Warszawa",
                },
                # Missing partitionId
                {
                    "partitionId": None,
                    "offerAbsoluteUri": "https://www.pracuj.pl/praca/bad,oferta,0",
                    "displayWorkplace": "Kraków",
                },
                # Missing URL
                {
                    "partitionId": 4003,
                    "offerAbsoluteUri": "",
                    "displayWorkplace": "Gdańsk",
                },
            ],
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = PracujScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["4001"]


def test_skips_groups_without_title(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([
            _group(title="Good"),
            _group(group_id="g-2", title=""),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    assert len(PracujScraper("any").fetch()) == 1


def test_returns_empty_when_no_next_data(httpx_mock) -> None:
    """The Cloudflare challenge page has no __NEXT_DATA__ — the parser
    must not crash, just return zero jobs."""
    httpx_mock.add_response(
        url=_LISTING_RE,
        text="<html><body>Cloudflare challenge</body></html>",
        is_reusable=True,
    )
    assert PracujScraper("any").fetch() == []


# --- pagination -------------------------------------------------------------


def test_paginates_until_three_consecutive_empty(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(group_id="a", offers=[
            {"partitionId": 100, "offerAbsoluteUri": "https://www.pracuj.pl/praca/a,oferta,100",
             "displayWorkplace": "Warszawa"},
        ])]),
    )
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=2",
        text=_next_data_html([_group(group_id="b", offers=[
            {"partitionId": 200, "offerAbsoluteUri": "https://www.pracuj.pl/praca/b,oferta,200",
             "displayWorkplace": "Kraków"},
        ])]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[3-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = PracujScraper("any", max_pages=20).fetch()
    assert {j.ats_id for j in jobs} == {"100", "200"}


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even with all-fresh content, ``max_pages`` is the hard ceiling."""
    for p in range(1, 4):
        httpx_mock.add_response(
            url=f"https://www.pracuj.pl/praca?pn={p}",
            text=_next_data_html([_group(group_id=f"g-{p}", offers=[
                {"partitionId": p * 100,
                 "offerAbsoluteUri": f"https://www.pracuj.pl/praca/x,oferta,{p * 100}",
                 "displayWorkplace": "Warszawa"},
            ])]),
        )
    jobs = PracujScraper("any", max_pages=3).fetch()
    assert len(jobs) == 3


# --- error handling ---------------------------------------------------------


def test_persistent_500_on_page_1_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        PracujScraper("any").fetch()


def test_500_on_later_page_keeps_collected_jobs(httpx_mock) -> None:
    """A 5xx on page 2+ shouldn't throw away what we already collected."""
    httpx_mock.add_response(
        url="https://www.pracuj.pl/praca?pn=1",
        text=_next_data_html([_group(group_id="a", offers=[
            {"partitionId": 100, "offerAbsoluteUri": "https://www.pracuj.pl/praca/a,oferta,100",
             "displayWorkplace": "Warszawa"},
        ])]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.pracuj\.pl/praca\?pn=[2-9]$"),
        status_code=500, is_reusable=True,
    )
    jobs = PracujScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["100"]
