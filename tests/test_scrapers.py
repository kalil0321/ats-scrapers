"""Tests for the per-ATS scrapers and the registry plumbing."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Several scrapers retry 3× on 429/5xx with a 1.5s base delay → up
    to 9s per failing test. Knock those down to 0 so tests stay fast."""
    for mod_name in ("greenhouse", "lever", "ashby"):
        try:
            mod = __import__(f"jobhive.scrapers.{mod_name}", fromlist=[""])
        except ImportError:
            continue
        if hasattr(mod, "MAX_RETRIES"):
            monkeypatch.setattr(mod, "MAX_RETRIES", 1)
        if hasattr(mod, "RETRY_BASE_DELAY"):
            monkeypatch.setattr(mod, "RETRY_BASE_DELAY", 0.0)

from jobhive.exceptions import CompanyNotFoundError, ScraperError  # noqa: E402
from jobhive.models import ATSType  # noqa: E402
from jobhive.scrapers import (  # noqa: E402
    AshbyScraper,
    BaseScraper,
    GreenhouseScraper,
    LeverScraper,
    ScraperRegistry,
    get_scraper,
)

# --- Registry ----------------------------------------------------------------

def test_registry_contains_known_scrapers() -> None:
    registered = ScraperRegistry.all()
    assert registered[ATSType.GREENHOUSE] is GreenhouseScraper
    assert registered[ATSType.LEVER] is LeverScraper
    assert registered[ATSType.ASHBY] is AshbyScraper


def test_registry_keys_are_valid_ats_types() -> None:
    """Every registered scraper must map to a real `ATSType`."""
    registered = ScraperRegistry.all()
    for ats in registered:
        assert isinstance(ats, ATSType)


def test_public_ats_types_are_registered() -> None:
    registered = set(ScraperRegistry.all())
    assert set(ATSType) - {ATSType.CUSTOM} == registered


def test_registry_covers_core_atses() -> None:
    """Sanity check: the core production ATSes always have a scraper."""
    registered = ScraperRegistry.all()
    core = {
        ATSType.GREENHOUSE,
        ATSType.LEVER,
        ATSType.ASHBY,
        ATSType.SMARTRECRUITERS,
        ATSType.WORKABLE,
        ATSType.RIPPLING,
        ATSType.WORKDAY,
    }
    assert core.issubset(set(registered.keys()))


def test_get_scraper_returns_instance() -> None:
    scraper = get_scraper("greenhouse", "openai")
    assert isinstance(scraper, GreenhouseScraper)
    assert scraper.company_slug == "openai"


def test_get_scraper_accepts_enum_too() -> None:
    scraper = get_scraper(ATSType.LEVER, "anthropic")
    assert isinstance(scraper, LeverScraper)


def test_get_scraper_unknown_ats_raises() -> None:
    with pytest.raises(ScraperError):
        get_scraper("custom", "openai")


def test_registry_returns_copy_so_external_mutation_is_safe() -> None:
    snapshot = ScraperRegistry.all()
    snapshot.pop(ATSType.GREENHOUSE, None)
    assert ATSType.GREENHOUSE in ScraperRegistry.all()


def test_register_decorator_adds_new_scraper() -> None:
    @ScraperRegistry.register(ATSType.CUSTOM)
    class TempScraper(BaseScraper):
        ats = ATSType.CUSTOM

        def fetch(self):
            return []

    try:
        assert ScraperRegistry.get(ATSType.CUSTOM) is TempScraper
    finally:
        ScraperRegistry._scrapers.pop(ATSType.CUSTOM, None)


# --- BaseScraper -------------------------------------------------------------

def test_base_scraper_repr() -> None:
    scraper = GreenhouseScraper("openai")
    assert repr(scraper) == "GreenhouseScraper('openai')"


def test_base_scraper_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        BaseScraper("x")  # type: ignore[abstract]


def test_base_scraper_default_timeout() -> None:
    scraper = GreenhouseScraper("openai")
    assert scraper.timeout == 30.0


def test_base_scraper_custom_timeout() -> None:
    scraper = GreenhouseScraper("openai", timeout=5.0)
    assert scraper.timeout == 5.0


# --- Greenhouse --------------------------------------------------------------

GH_SAMPLE = {
    "jobs": [
        {
            "id": 4567,
            "absolute_url": "https://boards.greenhouse.io/openai/jobs/4567",
            "title": "Software Engineer",
            "location": {"name": "San Francisco"},
            "updated_at": "2026-04-01T12:00:00Z",
        },
        {
            "id": 4568,
            "absolute_url": "https://boards.greenhouse.io/openai/jobs/4568",
            "title": "Research Scientist",
            "location": None,
            "updated_at": None,
        },
    ]
}


def test_greenhouse_parses_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/openai/jobs?content=true",
        json=GH_SAMPLE,
    )
    jobs = GreenhouseScraper("openai").fetch()
    assert len(jobs) == 2
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].location == "San Francisco"
    assert jobs[0].posted_at is not None
    assert jobs[1].location is None
    assert jobs[1].posted_at is None


def test_greenhouse_raises_company_not_found_on_404(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/missing/jobs?content=true",
        status_code=404,
    )
    with pytest.raises(CompanyNotFoundError):
        GreenhouseScraper("missing").fetch()


def test_greenhouse_raises_scraper_error_on_5xx(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/x/jobs?content=true",
        status_code=503,
        is_reusable=True,  # retry now fires; mock must satisfy all attempts
    )
    with pytest.raises(ScraperError):
        GreenhouseScraper("x").fetch()


def test_greenhouse_raises_on_network_failure(httpx_mock) -> None:
    import httpx

    httpx_mock.add_exception(
        httpx.ConnectError("boom"),
        url="https://boards-api.greenhouse.io/v1/boards/x/jobs?content=true",
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        GreenhouseScraper("x").fetch()


def test_greenhouse_handles_empty_jobs_list(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/empty/jobs?content=true",
        json={"jobs": []},
    )
    assert GreenhouseScraper("empty").fetch() == []


# --- Lever -------------------------------------------------------------------

LEVER_SAMPLE = [
    {
        "id": "abc-123",
        "hostedUrl": "https://jobs.lever.co/anthropic/abc-123",
        "text": "Backend Engineer",
        "categories": {"location": "Remote"},
        "createdAt": 1735689600000,  # 2025-01-01
    },
    {
        "id": "def-456",
        "hostedUrl": "https://jobs.lever.co/anthropic/def-456",
        "text": "Designer",
        "categories": None,
        "createdAt": None,
    },
]


def test_lever_parses_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/anthropic?mode=json",
        json=LEVER_SAMPLE,
    )
    jobs = LeverScraper("anthropic").fetch()
    assert len(jobs) == 2
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].location == "Remote"
    assert jobs[0].posted_at is not None
    assert jobs[1].location is None


def test_lever_404(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/missing?mode=json",
        status_code=404,
    )
    with pytest.raises(CompanyNotFoundError):
        LeverScraper("missing").fetch()


def test_lever_5xx(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/x?mode=json",
        status_code=500,
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        LeverScraper("x").fetch()


# --- Ashby -------------------------------------------------------------------

ASHBY_SAMPLE = {
    "jobs": [
        {
            "id": "job-uuid-1",
            "title": "Founding Engineer",
            "location": "New York",
            "jobUrl": "https://jobs.ashbyhq.com/ramp/job-uuid-1",
            "publishedAt": "2026-03-15T10:00:00Z",
            "compensation": {
                "compensationTierSummary": "$200K - $300K",
                "scrapeableCompensationSalarySummary": "$200K - $300K",
                "compensationTiers": [
                    {
                        "components": [
                            {
                                "compensationType": "Salary",
                                "interval": "1 YEAR",
                                "minValue": 200000,
                                "maxValue": 300000,
                                "currencyCode": "USD",
                            },
                            {
                                "compensationType": "EquityPercentage",
                                "interval": "NONE",
                                "minValue": None,
                                "maxValue": None,
                                "currencyCode": None,
                            },
                        ]
                    }
                ],
            },
        },
        {
            "id": "job-uuid-2",
            "title": "Product Designer",
            "location": "Remote",
            "applyUrl": "https://jobs.ashbyhq.com/ramp/job-uuid-2/apply",
            "publishedAt": None,
            "compensation": None,
        },
    ]
}


def test_ashby_parses_jobs_with_compensation(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true",
        json=ASHBY_SAMPLE,
    )
    jobs = AshbyScraper("ramp").fetch()
    assert len(jobs) == 2

    eng = jobs[0]
    assert eng.title == "Founding Engineer"
    assert eng.salary_currency == "USD"
    assert eng.salary_min == 200000
    assert eng.salary_max == 300000
    assert eng.salary_period == "YEAR"
    assert eng.salary_summary == "$200K - $300K"

    designer = jobs[1]
    assert designer.salary_currency is None
    assert str(designer.url).endswith("/apply")


def test_ashby_404(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.ashbyhq.com/posting-api/job-board/missing?includeCompensation=true",
        status_code=404,
    )
    with pytest.raises(CompanyNotFoundError):
        AshbyScraper("missing").fetch()


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("HOURLY", "HOUR"),
        ("DAILY", "DAY"),
        ("WEEKLY", "WEEK"),
        ("MONTHLY", "MONTH"),
        ("ANNUALLY", "YEAR"),
        ("YEARLY", "YEAR"),
    ],
)
def test_ashby_interval_mapping(httpx_mock, interval: str, expected: str) -> None:
    payload = {
        "jobs": [
            {
                "id": "x",
                "title": "X",
                "location": "Remote",
                "jobUrl": "https://jobs.ashbyhq.com/x/x",
                "publishedAt": None,
                "compensation": {
                    "compensationTiers": [
                        {
                            "components": [
                                {
                                    "compensationType": "Salary",
                                    "interval": interval,
                                    "minValue": 1,
                                    "maxValue": 2,
                                    "currencyCode": "USD",
                                }
                            ]
                        }
                    ]
                },
            }
        ]
    }
    httpx_mock.add_response(
        url="https://api.ashbyhq.com/posting-api/job-board/co?includeCompensation=true",
        json=payload,
    )
    jobs = AshbyScraper("co").fetch()
    assert jobs[0].salary_period == expected


def test_ashby_handles_compensation_without_tiers(httpx_mock) -> None:
    """Summary string surfaces even when structured tiers are absent."""
    payload = {
        "jobs": [
            {
                "id": "x",
                "title": "X",
                "location": "Remote",
                "jobUrl": "https://jobs.ashbyhq.com/co/x",
                "publishedAt": None,
                "compensation": {"compensationTierSummary": "Competitive"},
            }
        ]
    }
    httpx_mock.add_response(
        url="https://api.ashbyhq.com/posting-api/job-board/co?includeCompensation=true",
        json=payload,
    )
    jobs = AshbyScraper("co").fetch()
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_min is None
    assert jobs[0].salary_summary == "Competitive"
