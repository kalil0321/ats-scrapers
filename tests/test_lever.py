"""Tests for the Lever scraper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import LeverScraper, ScraperRegistry

API = "https://api.lever.co/v0/postings/acme?mode=json"


# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture
# in conftest.py — the shared fetch layer replaced per-scraper retry
# constants.


def _job(jid: str = "x1", text: str = "SWE",
         location: str = "Remote") -> dict:
    return {
        "id": jid,
        "text": text,
        "hostedUrl": f"https://jobs.lever.co/acme/{jid}",
        "categories": {"location": location, "team": "Eng"},
        "createdAt": 1714521600000,  # 2024-05-01T00:00:00Z
    }


def test_registry_resolves_lever() -> None:
    assert ScraperRegistry.get(ATSType.LEVER) is LeverScraper


def test_parses_basic_job(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=[_job()])
    jobs = LeverScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "SWE"
    assert job.company == "acme"
    assert job.location == "Remote"
    assert job.ats_id == "x1"


def test_returns_empty_list(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=[])
    assert LeverScraper("acme").fetch() == []


def test_404_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        LeverScraper("acme").fetch()


def test_5xx_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=502)
    httpx_mock.add_response(url=API, json=[_job()])
    assert len(LeverScraper("acme").fetch()) == 1


def test_posted_at_is_utc(httpx_mock) -> None:
    """``createdAt`` is epoch milliseconds; ``posted_at`` must be that
    instant in UTC, not the host's local wall-clock time."""
    httpx_mock.add_response(url=API, json=[_job()])
    job = LeverScraper("acme").fetch()[0]
    assert job.posted_at == datetime(2024, 5, 1, tzinfo=UTC)
    assert job.posted_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("country", "expected"),
    [("DE", "DE"), ("gb", "GB"), (" de ", "DE"), (None, None), ("", None), ("USA", None)],
)
def test_country_iso_from_posting_country(
    httpx_mock, country: str | None, expected: str | None,
) -> None:
    """Lever documents the posting-level ``country`` as an ISO 3166-1
    alpha-2 code (or null); it should land in ``country_iso``."""
    httpx_mock.add_response(url=API, json=[{**_job(), "country": country}])
    job = LeverScraper("acme").fetch()[0]
    assert job.country_iso == expected


# --- Regions ----------------------------------------------------------------

EU_API = "https://api.eu.lever.co/v0/postings/acme?mode=json"


def _eu_job(jid: str = "e1") -> dict:
    job = _job(jid)
    job["hostedUrl"] = f"https://jobs.eu.lever.co/acme/{jid}"
    return job


def test_bare_slug_is_the_us_region(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=[_job()])

    scraper = LeverScraper("acme")

    assert (scraper.tenant, scraper.region) == ("acme", "us")
    assert len(scraper.fetch()) == 1


def test_eu_region_reads_the_eu_api(httpx_mock) -> None:
    """The two regions do not share data: an EU board answers 404 on the
    US API."""
    httpx_mock.add_response(url=EU_API, json=[_eu_job()])

    jobs = LeverScraper("acme", region="eu").fetch()

    assert [job.ats_id for job in jobs] == ["e1"]
    assert jobs[0].company == "acme"
    assert str(jobs[0].url) == "https://jobs.eu.lever.co/acme/e1"
    assert [str(r.url) for r in httpx_mock.get_requests()] == [EU_API]


@pytest.mark.parametrize(
    ("slug", "region", "api"),
    [
        ("https://jobs.eu.lever.co/acme", "eu", EU_API),
        ("https://jobs.eu.lever.co/acme/", "eu", EU_API),
        ("https://jobs.eu.lever.co/acme/1b2c3d4e?lever-source=x", "eu", EU_API),
        ("https://JOBS.EU.LEVER.CO/acme", "eu", EU_API),
        ("https://jobs.lever.co/acme", "us", API),
        ("https://jobs.lever.co/acme/1b2c3d4e/apply", "us", API),
    ],
)
def test_board_url_gives_the_slug_and_the_region(httpx_mock, slug, region, api) -> None:
    httpx_mock.add_response(url=api, json=[_job()])

    scraper = LeverScraper(slug)

    assert (scraper.tenant, scraper.region) == ("acme", region)
    # The company is the board slug, not the URL.
    assert scraper.fetch()[0].company == "acme"


def test_region_is_case_insensitive_and_may_repeat_the_url(httpx_mock) -> None:
    httpx_mock.add_response(url=EU_API, json=[])

    scraper = LeverScraper("https://jobs.eu.lever.co/acme", region=" EU ")

    assert scraper.region == "eu"
    assert scraper.fetch() == []


def test_slug_case_is_kept(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.eu.lever.co/v0/postings/OpenPayd?mode=json", json=[]
    )

    assert LeverScraper("https://jobs.eu.lever.co/OpenPayd").fetch() == []


def test_slug_is_escaped_in_the_api_url(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/1840%26Company?mode=json", json=[]
    )

    assert LeverScraper("1840&Company").fetch() == []


@pytest.mark.parametrize(
    ("slug", "region"),
    [
        ("", None),
        ("   ", None),
        ("acme", "apac"),
        ("acme", "europe"),
        # A board URL names its own region.
        ("https://jobs.eu.lever.co/acme", "us"),
        ("https://jobs.lever.co/acme", "eu"),
        ("http://jobs.eu.lever.co/acme", None),
        ("https://jobs.eu.lever.co/", None),
        ("https://jobs.eu.lever.co", None),
        ("https://api.eu.lever.co/v0/postings/acme", None),
        ("https://jobs.eu.lever.co.evil.com/acme", None),
        ("https://user:pw@jobs.eu.lever.co/acme", None),
        ("https://jobs.eu.lever.co:8443/acme", None),
        ("https://example.com/acme", None),
    ],
)
def test_invalid_slug_or_region_is_rejected(slug, region) -> None:
    from ats_scrapers.exceptions import ScraperError

    with pytest.raises(ScraperError):
        LeverScraper(slug, region=region)


def test_catalogue_slug_of_an_eu_row_selects_the_eu_region(httpx_mock) -> None:
    """``find_company()`` hands out the ``slug`` column for
    ``get_scraper("lever", slug)``: for a board in the EU region that is
    the board URL, never a bare slug (which is read from the US API)."""
    import csv
    from pathlib import Path

    from ats_scrapers.scrapers import get_scraper

    with Path("ats-companies/lever.csv").open(newline="") as handle:
        eu_rows = [row for row in csv.DictReader(handle) if "jobs.eu.lever.co" in row["url"]]

    assert eu_rows
    for row in eu_rows:
        scraper = get_scraper("lever", row["slug"])
        assert scraper.region == "eu"
        assert row["url"] == f"https://jobs.eu.lever.co/{scraper.tenant}"


def test_us_404_points_at_the_eu_region_and_does_not_try_it(httpx_mock) -> None:
    """The same slug can be another company in the other region, so a
    missing US board is not looked up there."""
    httpx_mock.add_response(url=API, status_code=404)

    with pytest.raises(CompanyNotFoundError, match="region='eu'"):
        LeverScraper("acme").fetch()
    assert [str(r.url) for r in httpx_mock.get_requests()] == [API]


def test_eu_404_is_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=EU_API, status_code=404)

    with pytest.raises(CompanyNotFoundError) as excinfo:
        LeverScraper("acme", region="eu").fetch()
    assert "region='eu'" not in str(excinfo.value)
