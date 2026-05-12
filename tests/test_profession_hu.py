"""Tests for the Profession.hu (Hungary) scraper.

Profession.hu's listing pages embed a ``dataLayer.push({"event":
"view_item_list", ...})`` block that carries every field the canonical
schema needs (title, employer, employment-type, category, sub-category,
location, salary-visibility, experience bucket). The scraper parses
that block plus the matching ``<li data-prof-id … data-link …>`` tags
to recover absolute detail URLs — no per-job detail fetches required.

These tests pin:
  - registry wiring
  - dataLayer + data-link extraction (id → absolute URL)
  - location-id underscore handling and ``country_iso="HU"``
  - employment_type map for both English (live) and Hungarian
    (defensive) labels
  - salary visibility flag → ``salary_summary`` / ``salary_currency``
  - pagination by total-rows count and ``max_pages`` cap
  - HTTP retry / failure behaviour
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ProfessionHuScraper, ScraperRegistry

_LISTING_URL_RE = re.compile(r"^https://www\.profession\.hu/allasok/\d+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.profession_hu as p
    monkeypatch.setattr(p, "MAX_RETRIES", 1)
    monkeypatch.setattr(p, "RETRY_BASE_DELAY", 0.0)


# --- fixture builders -------------------------------------------------------


def _item(
    *,
    item_id: int = 2898489,
    item_name: str = "BPO Projekt koordinátor",
    affiliation: str = "Trenkwalder",
    category: str = "Gyártás, Termelés",
    category2: str = "Projektmenedzsment",
    category3: str = "full time",
    category4: str = "3-5 years experience",
    item_variant: str = "salary confidential",
    location_id: str = "Heves_megye,_Gyöngyös",
    application_type: str = "karrierlink",
    prof_product_name: str = "normal",
) -> dict[str, Any]:
    return {
        "item_name": item_name,
        "item_id": item_id,
        "item_brand": "classified listing",
        "item_category": category,
        "item_category2": category2,
        "item_category3": category3,
        "item_category4": category4,
        "item_list_name": (
            "Állások, munkák és állásajánlatok - 18750 db - "
            "2026 Május | Profession.hu"
        ),
        "item_list_id": "classified_search_results",
        "item_variant": item_variant,
        "location_id": location_id,
        "index": 1,
        "affiliation": affiliation,
        "quantity": 1,
        "price": None,
        "application_type": application_type,
        "prof_product_name": prof_product_name,
        "ai_search_list": "0",
    }


def _build_listing_html(
    items: list[dict[str, Any]],
    *,
    total_label: str | None = None,
) -> str:
    """Mirror the live listing-page shape closely enough that the
    scraper's regexes match: a ``dataLayer.push`` with the
    ``view_item_list`` payload, plus ``<li data-prof-id data-link>``
    rows for each item."""
    # Patch the total count into every item's item_list_name so the
    # ``-{N} db-`` regex sees the value the test wants. Default to a
    # total that exactly matches the visible row count, so single-page
    # tests don't trigger pagination unless they ask for it.
    real_total = total_label or f"{len(items)} db"
    for it in items:
        if isinstance(it, dict):
            it["item_list_name"] = (
                f"Állások, munkák és állásajánlatok - {real_total} - "
                "2026 Május | Profession.hu"
            )
    payload = {
        "event": "view_item_list",
        "subProperty": "Application",
        "ecommerce": {"currency": "HUF", "items": items},
    }
    blob = json.dumps(payload, ensure_ascii=False)
    li_rows = "\n".join(
        f'<li class="advertisement-result-list-item" '
        f'data-prof-id="{it["item_id"]}" '
        f'data-link="https://www.profession.hu/allas/job-{it["item_id"]}'
        f'?sessionId=abc">'
        for it in items
        if "item_id" in it
    )
    return (
        "<html><body>"
        + li_rows
        + "<script>dataLayer.push("
        + blob
        + ");</script>"
        + "</body></html>"
    )


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_profession_hu() -> None:
    assert ScraperRegistry.get(ATSType.PROFESSIONHU) is ProfessionHuScraper


def test_ats_type_value() -> None:
    assert ATSType.PROFESSIONHU == "profession_hu"


# --- happy path -------------------------------------------------------------


def test_parses_listing_item_with_full_payload(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html([_item()]),
    )
    jobs = ProfessionHuScraper("any").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.PROFESSIONHU
    assert job.ats_id == "2898489"
    assert job.global_id == "profession_hu:2898489"
    assert job.title == "BPO Projekt koordinátor"
    assert job.company == "Trenkwalder"
    assert job.country_iso == "HU"
    assert job.region == "Europe"
    assert job.language == "hu"
    assert job.employment_type == "FULL_TIME"
    # location_id 'Heves_megye,_Gyöngyös' → 'Heves megye, Gyöngyös'
    assert job.location == "Heves megye, Gyöngyös"
    # data-link survives — including the sessionId query string
    assert str(job.url).startswith(
        "https://www.profession.hu/allas/job-2898489"
    )
    assert "sessionId=abc" in str(job.url)
    # Confidential salary → don't synthesise a numeric range or label.
    assert job.salary_currency is None
    assert job.salary_summary is None
    assert job.raw is not None
    assert job.raw["category"] == "Gyártás, Termelés"
    assert job.raw["industry"] == "Projektmenedzsment"
    assert job.raw["modality"] == "normal"
    assert job.raw["salary_visibility"] == "salary confidential"


# --- employment type map ---------------------------------------------------


@pytest.mark.parametrize(
    "category3,expected",
    [
        ("full time", "FULL_TIME"),
        ("part time", "PART_TIME"),
        ("contract", "CONTRACT"),
        ("internship", "INTERN"),
        ("temporary", "TEMPORARY"),
        # Defensive: Hungarian originals also map.
        ("Teljes munkaidős", "FULL_TIME"),
        ("Részmunkaidős", "PART_TIME"),
        ("Gyakornoki", "INTERN"),
    ],
)
def test_employment_type_map(
    httpx_mock, category3: str, expected: str
) -> None:
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(
            [_item(item_id=1, category3=category3)]
        ),
    )
    jobs = ProfessionHuScraper("any").fetch()
    assert jobs[0].employment_type == expected


def test_unknown_employment_type_is_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(
            [_item(item_id=1, category3="weekends only")]
        ),
    )
    assert ProfessionHuScraper("any").fetch()[0].employment_type is None


# --- salary visibility -----------------------------------------------------


def test_salary_publicised_sets_summary_and_huf_currency(httpx_mock) -> None:
    """The listing's ``item_variant`` only flags visibility, not a
    numeric range. When publicised we surface ``HUF`` + a Hungarian
    'banded salary' label so consumers can filter for paid postings;
    the actual min/max only ships on the detail page."""
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(
            [_item(item_id=1, item_variant="salary publicised")]
        ),
    )
    job = ProfessionHuScraper("any").fetch()[0]
    assert job.salary_currency == "HUF"
    assert job.salary_summary == "Sávos bérezés"


def test_salary_confidential_leaves_fields_blank(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(
            [_item(item_id=1, item_variant="salary confidential")]
        ),
    )
    job = ProfessionHuScraper("any").fetch()[0]
    assert job.salary_currency is None
    assert job.salary_summary is None


# --- location handling -----------------------------------------------------


def test_empty_location_id_falls_back_to_none(httpx_mock) -> None:
    """Country-wide / remote postings ship location_id="". The scraper
    leaves ``location`` null so downstream LLM enrichment can apply its
    own heuristics rather than misclassify the row."""
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html([_item(item_id=1, location_id="")]),
    )
    job = ProfessionHuScraper("any").fetch()[0]
    assert job.location is None
    assert job.country_iso == "HU"  # Country still pinned.


def test_location_id_underscore_to_space(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(
            [_item(item_id=1, location_id="Pest_megye,_Budapest")]
        ),
    )
    assert ProfessionHuScraper("any").fetch()[0].location == (
        "Pest megye, Budapest"
    )


# --- URL fallback ----------------------------------------------------------


def test_url_falls_back_to_slugless_when_data_link_absent(
    httpx_mock,
) -> None:
    """When a listing row is rendered without the standard <li> wrapper
    (rare — has happened on highlighted ads) the scraper still emits a
    job, using the slug-less ``/allas/{id}`` URL form which the site
    301-redirects to the full slug page."""
    # Hand-craft a listing body with the dataLayer but no <li> rows.
    item = _item(item_id=42)
    item["item_list_name"] = (
        "Állások, munkák és állásajánlatok - 1 db - 2026 | Profession.hu"
    )
    payload = {
        "event": "view_item_list",
        "ecommerce": {"items": [item]},
    }
    html = (
        "<html><body><script>dataLayer.push("
        + json.dumps(payload, ensure_ascii=False)
        + ");</script></body></html>"
    )
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1", text=html
    )
    job = ProfessionHuScraper("any").fetch()[0]
    assert str(job.url) == "https://www.profession.hu/allas/42"


# --- pagination ------------------------------------------------------------


def test_paginates_until_total_count(httpx_mock) -> None:
    """Page 1's dataLayer header advertises the total (``- 50 db -``).
    With 20 rows per page that's 3 pages — the scraper fans out the
    remaining 2 in parallel."""
    page1_items = [_item(item_id=i) for i in range(1, 21)]
    page2_items = [_item(item_id=i) for i in range(21, 41)]
    page3_items = [_item(item_id=i) for i in range(41, 51)]
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(page1_items, total_label="50 db"),
    )
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/2",
        text=_build_listing_html(page2_items, total_label="50 db"),
    )
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/3",
        text=_build_listing_html(page3_items, total_label="50 db"),
    )
    jobs = ProfessionHuScraper("any").fetch()
    assert len(jobs) == 50


def test_no_fanout_when_total_fits_one_page(httpx_mock) -> None:
    items = [_item(item_id=i) for i in range(1, 11)]
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(items, total_label="10 db"),
    )
    # No page 2 — if requested, httpx_mock would error.
    assert len(ProfessionHuScraper("any").fetch()) == 10


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even if the header says 10 000 rows (500 pages), ``max_pages=2``
    must stop after the probe + 1 fan-out."""
    items = [_item(item_id=i) for i in range(1, 21)]
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(items, total_label="10000 db"),
    )
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/2",
        text=_build_listing_html(
            [_item(item_id=i) for i in range(21, 41)],
            total_label="10000 db",
        ),
    )
    # Page 3 must NOT be requested.
    jobs = ProfessionHuScraper("any", max_pages=2).fetch()
    assert len(jobs) == 40


def test_dedupes_across_pages(httpx_mock) -> None:
    """If two pages happen to surface the same ``item_id`` (race
    conditions on the ranking re-sort) the row should appear once."""
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(
            [_item(item_id=1), _item(item_id=2)],
            total_label="40 db",
        ),
    )
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/2",
        text=_build_listing_html(
            [_item(item_id=2), _item(item_id=3)],
            total_label="40 db",
        ),
    )
    jobs = ProfessionHuScraper("any").fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "3"]


# --- defensive -------------------------------------------------------------


def test_missing_datalayer_yields_empty_page(httpx_mock) -> None:
    """Pages past the real pagination cap render an empty results
    section without a ``view_item_list`` push. Returning an empty list
    (rather than crashing) lets the scraper survive flaky upper-bound
    estimates."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        text="<html><body><p>Nincs találat</p></body></html>",
        is_reusable=True,
    )
    # max_pages=1 so we only hit page 1.
    assert ProfessionHuScraper("any", max_pages=1).fetch() == []


def test_drops_item_with_no_id_or_title(httpx_mock) -> None:
    items = [
        _item(item_id=1),
        # Missing item_id entirely
        {"item_name": "ghost", "affiliation": "x"},
        # Empty title
        _item(item_id=3, item_name=""),
    ]
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(items, total_label="3 db"),
    )
    jobs = ProfessionHuScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_missing_affiliation_falls_back_to_unknown(httpx_mock) -> None:
    items = [_item(item_id=1, affiliation="")]
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(items),
    )
    assert ProfessionHuScraper("any").fetch()[0].company == "Unknown"


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL_RE, status_code=500, is_reusable=True
    )
    with pytest.raises(ScraperError):
        ProfessionHuScraper("any").fetch()


def test_one_page_failure_does_not_abort_whole_scrape(
    httpx_mock,
) -> None:
    """A 5xx on a fan-out page is logged and skipped — the probe page's
    rows still come back."""
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/1",
        text=_build_listing_html(
            [_item(item_id=i) for i in range(1, 21)],
            total_label="40 db",
        ),
    )
    # Page 2 fails persistently
    httpx_mock.add_response(
        url="https://www.profession.hu/allasok/2",
        status_code=500,
        is_reusable=True,
    )
    jobs = ProfessionHuScraper("any").fetch()
    # 20 rows from page 1 survived.
    assert len(jobs) == 20
