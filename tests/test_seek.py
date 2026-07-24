"""Tests for the SEEK / JobsDB / JobStreet APAC scraper.

The scraper covers eight regional sites (au, nz, hk, th, my, id, ph,
sg) that all share the same ``chalice-search v5`` JSON API. Tests
verify:

- region → (host, siteKey, country_iso) mapping
- ``_parse_job`` populates the canonical Job fields correctly
- country_iso / salary_currency / region are inferred from the region
- pagination follows ``totalCount`` and stops cleanly

All network access is mocked via ``httpx_mock``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, SeekScraper
from ats_scrapers.scrapers.seek import SEEK_SITES, _parse_iso8601


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.seek as seek_mod

    monkeypatch.setattr(seek_mod, "MAX_RETRIES", 1)
    monkeypatch.setattr(seek_mod, "RETRY_BASE_DELAY", 0.0)


# --- fixtures ---------------------------------------------------------------


def _au_job(**overrides) -> dict:
    base = {
        "id": "91798287",
        "title": "Operations Manager",
        "companyName": "Go 3 pl",
        "advertiser": {"id": "63113577", "description": "Go 3 pl"},
        "bulletPoints": [
            "Large 30,000m² 3PL site",
            "Fast-paced hands-on operations",
        ],
        "teaser": "Hands-on Warehouse Ops Manager for busy 3PL in Braeside.",
        "classifications": [
            {
                "classification": {
                    "id": "6092",
                    "description": "Manufacturing, Transport & Logistics",
                },
                "subclassification": {
                    "id": "6102",
                    "description": "Management",
                },
            }
        ],
        "locations": [
            {
                "label": "Braeside, Melbourne VIC",
                "countryCode": "AU",
            }
        ],
        "salaryLabel": "$115,000 – $125,000 per year",
        "listingDate": "2026-04-29T00:23:37Z",
        "listingDateDisplay": "13d ago",
        "workTypes": ["Full time"],
        "workArrangements": {
            "data": [{"id": "1", "label": {"text": "On-site"}}]
        },
        "roleId": "operations-manager",
        "displayType": "standard",
        "isFeatured": False,
    }
    base.update(overrides)
    return base


def _sg_job(**overrides) -> dict:
    base = {
        "id": "78311013",
        "title": "Backend Engineer",
        "companyName": "Acme Pte Ltd",
        "advertiser": {"id": "12345", "description": "Acme Pte Ltd"},
        "bulletPoints": ["Modern stack", "Hybrid working"],
        "teaser": "Join our growing backend team.",
        "classifications": [
            {
                "classification": {
                    "id": "6281",
                    "description": "Information & Communication Technology",
                },
            }
        ],
        "locations": [
            {"label": "Tampines North, East Region", "countryCode": "SG"}
        ],
        "salaryLabel": "S$8,000 - S$12,000 per month",
        "listingDate": "2026-05-10T09:00:00Z",
    }
    base.update(overrides)
    return base


def _page(jobs: list[dict], total: int | None = None) -> dict:
    return {
        "data": jobs,
        "totalCount": total if total is not None else len(jobs),
        "info": {},
        "facets": {},
        "searchParams": {},
        "sortModes": [],
    }


def _search_url(host: str, site_key: str, page: int) -> re.Pattern[str]:
    """Match the search URL for a given host/siteKey/page regardless of
    parameter order (httpx serializes querystring deterministically but
    the test should not depend on it)."""
    return re.compile(
        rf"^https://{re.escape(host)}/api/jobsearch/v5/search\?"
        rf"(?=.*siteKey={re.escape(site_key)})"
        rf"(?=.*page={page}\b)"
    )


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_seek() -> None:
    assert ScraperRegistry.get(ATSType.SEEK) is SeekScraper


def test_eight_regions_in_sites_map() -> None:
    """If somebody drops a region, the dataset coverage shrinks
    silently. Pin the eight regions explicitly."""
    assert set(SEEK_SITES) == {
        "au", "nz", "hk", "th", "my", "id", "ph", "sg",
    }


def test_unknown_region_rejected() -> None:
    with pytest.raises(ScraperError, match="Unknown SEEK region"):
        SeekScraper("zz")


def test_all_region_expands_to_every_site() -> None:
    s = SeekScraper("all")
    assert set(s.regions) == set(SEEK_SITES)


def test_single_region_is_singleton_tuple() -> None:
    s = SeekScraper("au")
    assert s.regions == ("au",)


def test_region_is_case_insensitive() -> None:
    assert SeekScraper("AU").regions == ("au",)
    assert set(SeekScraper("ALL").regions) == set(SEEK_SITES)


# --- _parse_iso8601 ---------------------------------------------------------


def test_parse_iso8601_handles_z_suffix() -> None:
    dt = _parse_iso8601("2026-04-29T00:23:37Z")
    assert dt == datetime(2026, 4, 29, 0, 23, 37, tzinfo=UTC)


def test_parse_iso8601_handles_offset() -> None:
    dt = _parse_iso8601("2026-04-29T03:23:37+03:00")
    assert dt == datetime(2026, 4, 29, 0, 23, 37, tzinfo=UTC)


def test_parse_iso8601_rejects_naive_timestamp() -> None:
    assert _parse_iso8601("2026-04-29T00:23:37") is None


def test_parse_iso8601_returns_none_for_garbage() -> None:
    assert _parse_iso8601("not a date") is None
    assert _parse_iso8601(None) is None
    assert _parse_iso8601("") is None


# --- _parse_job per region --------------------------------------------------


def test_parse_au_job_populates_canonical_fields() -> None:
    scraper = SeekScraper("au")
    job = scraper._parse_job(
        _au_job(), host="au.seek.com", country_iso="AU"
    )
    assert job is not None
    assert job.ats_type is ATSType.SEEK
    assert job.ats_id == "91798287"
    assert job.global_id == "seek:91798287"
    assert job.title == "Operations Manager"
    assert job.company == "Go 3 pl"
    assert str(job.url) == "https://au.seek.com/job/91798287"
    assert job.location == "Braeside, Melbourne VIC"
    assert job.country_iso == "AU"
    assert job.region == "Oceania"
    assert job.language == "en"
    # Salary label + AU region → AUD currency, period YEAR.
    assert job.salary_currency == "AUD"
    assert job.salary_period == "YEAR"
    assert job.salary_summary == "$115,000 – $125,000 per year"
    assert job.department == "Manufacturing, Transport & Logistics"
    # Description: teaser + bullets, no HTML.
    assert job.description is not None
    assert "Hands-on Warehouse Ops Manager" in job.description
    assert "Large 30,000m" in job.description
    assert job.posted_at == datetime(
        2026, 4, 29, 0, 23, 37, tzinfo=UTC
    )


def test_parse_sg_job_uses_sgd_currency() -> None:
    scraper = SeekScraper("sg")
    job = scraper._parse_job(
        _sg_job(), host="sg.jobstreet.com", country_iso="SG"
    )
    assert job is not None
    assert job.country_iso == "SG"
    assert job.region == "Asia"
    assert job.salary_currency == "SGD"
    assert str(job.url) == "https://sg.jobstreet.com/job/78311013"


@pytest.mark.parametrize(
    "region,host,country,currency,continent",
    [
        ("au", "au.seek.com", "AU", "AUD", "Oceania"),
        ("nz", "www.seek.co.nz", "NZ", "NZD", "Oceania"),
        ("hk", "hk.jobsdb.com", "HK", "HKD", "Asia"),
        ("th", "th.jobsdb.com", "TH", "THB", "Asia"),
        ("my", "my.jobstreet.com", "MY", "MYR", "Asia"),
        ("id", "id.jobstreet.com", "ID", "IDR", "Asia"),
        ("ph", "ph.jobstreet.com", "PH", "PHP", "Asia"),
        ("sg", "sg.jobstreet.com", "SG", "SGD", "Asia"),
    ],
)
def test_parse_job_per_region_iso_and_currency(
    region: str, host: str, country: str, currency: str, continent: str,
) -> None:
    """Every region resolves to the right country code, ISO 4217 currency,
    and Oceania/Asia continent."""
    scraper = SeekScraper(region)
    payload = _au_job(
        id=f"id-{region}",
        locations=[{"label": "Somewhere", "countryCode": country}],
        salaryLabel="some salary",
    )
    job = scraper._parse_job(payload, host=host, country_iso=country)
    assert job is not None
    assert job.country_iso == country
    assert job.salary_currency == currency
    assert job.region == continent
    assert str(job.url).startswith(f"https://{host}/job/")


def test_parse_job_no_salary_means_no_currency() -> None:
    """``salary_currency`` is only set when a salary label is present —
    we don't invent a currency for empty rows."""
    scraper = SeekScraper("au")
    payload = _au_job(salaryLabel="")
    job = scraper._parse_job(payload, host="au.seek.com", country_iso="AU")
    assert job is not None
    assert job.salary_summary is None
    assert job.salary_currency is None
    assert job.salary_period is None


def test_parse_job_falls_back_to_company_name_when_advertiser_missing() -> None:
    scraper = SeekScraper("au")
    payload = _au_job(advertiser={})
    job = scraper._parse_job(payload, host="au.seek.com", country_iso="AU")
    assert job is not None
    assert job.company == "Go 3 pl"  # from companyName


def test_parse_job_unknown_company_when_both_advertiser_and_name_missing() -> None:
    scraper = SeekScraper("au")
    payload = _au_job(advertiser={}, companyName=None)
    job = scraper._parse_job(payload, host="au.seek.com", country_iso="AU")
    assert job is not None
    assert job.company == "Unknown"


def test_parse_job_uses_location_country_when_present() -> None:
    """Some postings carry a location countryCode that differs from
    the region — pass-through that value rather than the region's
    default."""
    scraper = SeekScraper("au")
    payload = _au_job(
        locations=[{"label": "Auckland", "countryCode": "NZ"}],
    )
    job = scraper._parse_job(payload, host="au.seek.com", country_iso="AU")
    assert job is not None
    assert job.country_iso == "NZ"
    # NZ has its own currency mapping even though we're on the AU site.
    assert job.salary_currency == "NZD"


def test_parse_job_overseas_country_does_not_claim_asia() -> None:
    scraper = SeekScraper("sg")
    payload = _sg_job(
        locations=[{"label": "London", "countryCode": "GB"}],
    )
    job = scraper._parse_job(
        payload,
        host="sg.jobstreet.com",
        country_iso="SG",
    )
    assert job is not None
    assert job.country_iso == "GB"
    assert job.region is None


def test_parse_job_returns_none_for_empty_id() -> None:
    scraper = SeekScraper("au")
    assert scraper._parse_job(
        _au_job(id=""), host="au.seek.com", country_iso="AU"
    ) is None
    assert scraper._parse_job(
        _au_job(title=""), host="au.seek.com", country_iso="AU"
    ) is None


def test_parse_job_raw_payload_is_compact() -> None:
    scraper = SeekScraper("au")
    job = scraper._parse_job(
        _au_job(), host="au.seek.com", country_iso="AU"
    )
    assert job is not None and job.raw is not None
    # Heavy fields like solMetadata / tracking should NOT appear.
    assert "solMetadata" not in job.raw
    assert "tracking" not in job.raw
    # Identifier fields we do keep.
    assert job.raw.get("advertiserId") == "63113577"
    assert job.raw.get("workArrangements") == ["On-site"]


def test_parse_job_description_handles_missing_bullets() -> None:
    scraper = SeekScraper("au")
    payload = _au_job(bulletPoints=[])
    job = scraper._parse_job(payload, host="au.seek.com", country_iso="AU")
    assert job is not None
    assert job.description == "Hands-on Warehouse Ops Manager for busy 3PL in Braeside."


def test_parse_job_description_handles_missing_teaser() -> None:
    scraper = SeekScraper("au")
    payload = _au_job(teaser="")
    job = scraper._parse_job(payload, host="au.seek.com", country_iso="AU")
    assert job is not None
    assert job.description is not None
    assert job.description.startswith("- ")


def test_description_uses_canonical_25k_cap() -> None:
    scraper = SeekScraper("au")
    job = scraper._parse_job(
        _au_job(teaser="x" * 30_000, bulletPoints=[]),
        host="au.seek.com",
        country_iso="AU",
    )
    assert job is not None and job.description is not None
    assert len(job.description) == 25_000


# --- end-to-end pagination via httpx_mock -----------------------------------


def test_fetch_single_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_search_url("au.seek.com", "AU-Main", 1),
        json=_page([_au_job(id="1"), _au_job(id="2")], total=2),
    )
    jobs = SeekScraper("au").fetch()
    assert len(jobs) == 2
    assert {j.ats_id for j in jobs} == {"1", "2"}


def test_fetch_paginates_until_total_reached(httpx_mock) -> None:
    # Total 150 → 2 pages of 100. With ``is_reusable=True`` we let
    # httpx_mock match by page index in the URL.
    httpx_mock.add_response(
        url=_search_url("au.seek.com", "AU-Main", 1),
        json=_page(
            [_au_job(id=f"p1-{i}") for i in range(100)], total=150,
        ),
    )
    httpx_mock.add_response(
        url=_search_url("au.seek.com", "AU-Main", 2),
        json=_page(
            [_au_job(id=f"p2-{i}") for i in range(50)], total=150,
        ),
    )
    jobs = SeekScraper("au").fetch()
    assert len(jobs) == 150
    ids = {j.ats_id for j in jobs}
    assert "p1-0" in ids and "p2-49" in ids


def test_short_page_stops_scheduling_later_pages(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.seek as module

    monkeypatch.setattr(module, "MAX_CONCURRENCY", 1)
    httpx_mock.add_response(
        url=_search_url("au.seek.com", "AU-Main", 1),
        json=_page([_au_job(id=f"p1-{i}") for i in range(100)], total=300),
    )
    httpx_mock.add_response(
        url=_search_url("au.seek.com", "AU-Main", 2),
        json=_page([_au_job(id="p2")], total=300),
    )
    jobs = SeekScraper("au").fetch()
    assert len(jobs) == 101
    assert len(httpx_mock.get_requests()) == 2


def test_short_page_does_not_request_later_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_search_url("au.seek.com", "AU-Main", 1),
        json=_page([_au_job(id=f"p1-{i}") for i in range(100)], total=300),
    )
    httpx_mock.add_response(
        url=_search_url("au.seek.com", "AU-Main", 2),
        json=_page([_au_job(id="p2")], total=300),
    )
    jobs = SeekScraper("au").fetch()

    assert len(jobs) == 101
    assert jobs[-1].ats_id == "p2"
    assert len(httpx_mock.get_requests()) == 2


def test_fetch_dedupes_cross_region_for_all(httpx_mock) -> None:
    """Two regions both return job id=999 — only one survives the
    cross-region dedup in ``_fetch_async``."""
    shared = _au_job(id="999")
    for region, (host, site_key, _) in SEEK_SITES.items():
        httpx_mock.add_response(
            url=_search_url(host, site_key, 1),
            json=_page([shared], total=1),
        )
        del region  # only the URL matters
    jobs = SeekScraper("all").fetch()
    # All 8 regions returned the same row → only one in the final list.
    assert len(jobs) == 1
    assert jobs[0].ats_id == "999"
