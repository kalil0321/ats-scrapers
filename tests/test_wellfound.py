"""Tests for the Wellfound scraper (role pages with embedded Apollo data)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers import ScraperRegistry, WellfoundScraper
from ats_scrapers.scrapers.wellfound import _parse_salary

_ROLE_RE = re.compile(r"^https://wellfound\.com/role/")
_FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"


def _listing(job_id: int, **overrides: Any) -> dict[str, Any]:
    listing = {
        "__typename": "JobListingSearchResult",
        "id": str(job_id),
        "slug": f"engineer-{job_id}",
        "title": f"Engineer {job_id}",
        "description": f"Build things ({job_id}).",
        "locationNames": ["San Francisco", "New York"],
        "acceptedRemoteLocationNames": [],
        "remote": False,
        "compensation": "$120k – $160k • 0.1% – 0.5%",
        "jobType": "full-time",
        "liveStartAt": 1793665422,
        "yearsExperienceMin": 3,
        "yearsExperienceMax": None,
        "primaryRoleTitle": "Software Engineer",
        "atsSource": None,
        "autoPosted": False,
    }
    listing.update(overrides)
    return listing


def _role_page(
    role: str,
    page: int = 1,
    *,
    page_count: int = 1,
    startups: list[tuple[str, list[dict[str, Any]]]] | None = None,
) -> str:
    """A role page as Wellfound renders it: the Apollo cache in __NEXT_DATA__."""
    if startups is None:
        startups = [("Acme", [_listing(page)])]
    cache: dict[str, Any] = {}
    startup_refs = []
    for index, (name, listings) in enumerate(startups):
        key = f"StartupResult:{page}{index}"
        for listing in listings:
            cache[f"JobListingSearchResult:{listing['id']}"] = listing
        cache[key] = {
            "id": f"{page}{index}",
            "name": name,
            "slug": name.lower(),
            "highlightedJobListings": [
                {"__ref": f"JobListingSearchResult:{listing['id']}"} for listing in listings
            ],
        }
        startup_refs.append({"__ref": key})
    args = json.dumps({"page": page, "role": role}, separators=(",", ":"))
    cache["ROOT_QUERY"] = {
        "talent": {
            f"seoLandingPageJobSearchResults({args})": {
                "totalJobCount": 999,
                "pageCount": page_count,
                "perPage": 20,
                "startups": startup_refs,
            }
        }
    }
    data = {"props": {"pageProps": {"apolloState": {"data": cache}}}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data)}</script></html>"
    )


def _role_url(role: str, page: int = 1) -> str:
    url = f"https://wellfound.com/role/{role}"
    return url if page == 1 else f"{url}?page={page}"


def _scraper(*roles: str, **kwargs: Any) -> WellfoundScraper:
    return WellfoundScraper(
        "any", role_slugs=roles or ("software-engineer",), request_interval=0, **kwargs
    )


def test_registry_resolves_wellfound() -> None:
    assert ScraperRegistry.get(ATSType.WELLFOUND) is WellfoundScraper


def test_parses_a_listing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_role_url("software-engineer"), text=_role_page("software-engineer")
    )

    [job] = _scraper().fetch()

    assert str(job.url) == "https://wellfound.com/jobs/1-engineer-1"
    assert job.ats_id == "1"
    assert job.title == "Engineer 1"
    assert job.company == "Acme"
    assert job.location == "San Francisco, New York"
    assert job.is_remote is False
    assert job.salary_summary == "$120k – $160k • 0.1% – 0.5%"
    assert (job.salary_currency, job.salary_min, job.salary_max) == ("USD", 120_000, 160_000)
    assert job.salary_period == "YEAR"
    assert job.employment_type == "FULL_TIME"
    assert job.experience == 3
    assert job.description == "Build things (1)."
    assert job.posted_at == datetime.fromtimestamp(1793665422, UTC)
    assert job.raw == {"company_slug": "acme", "company_id": "10", "role": "Software Engineer"}


def test_user_agent_is_not_the_httpx_default(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_role_url("software-engineer"), text=_role_page("software-engineer")
    )
    _scraper().fetch()
    assert not httpx_mock.get_request().headers["User-Agent"].startswith("python-httpx")


def test_reads_every_page_and_dedupes_across_roles(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_role_url("software-engineer"),
        text=_role_page("software-engineer", 1, page_count=3),
    )
    httpx_mock.add_response(
        url=_role_url("software-engineer", 2),
        text=_role_page("software-engineer", 2, page_count=3),
    )
    httpx_mock.add_response(
        url=_role_url("software-engineer", 3),
        text=_role_page("software-engineer", 3, page_count=3),
    )
    httpx_mock.add_response(
        url=_role_url("data-engineer"),
        text=_role_page(
            "data-engineer", startups=[("Beta", [_listing(2), _listing(9)])]
        ),
    )

    jobs = _scraper("software-engineer", "data-engineer").fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3", "9"]


def test_a_page_past_the_last_one_ends_the_role(httpx_mock) -> None:
    # The page count on page 1 can be stale; later pages then redirect to page 1.
    httpx_mock.add_response(
        url=_role_url("software-engineer"),
        text=_role_page("software-engineer", 1, page_count=3),
    )
    httpx_mock.add_response(
        url=_role_url("software-engineer", 2),
        text=_role_page("software-engineer", 2, page_count=2),
    )
    httpx_mock.add_response(
        url=_role_url("software-engineer", 3),
        status_code=303,
        headers={"Location": _role_url("software-engineer")},
    )

    jobs = _scraper().fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]


def test_max_pages_limits_the_pages_read(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_role_url("software-engineer"),
        text=_role_page("software-engineer", 1, page_count=50),
    )
    httpx_mock.add_response(
        url=_role_url("software-engineer", 2),
        text=_role_page("software-engineer", 2, page_count=50),
    )

    jobs = _scraper(max_pages=2).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]


@pytest.mark.parametrize(
    "overrides",
    [{"atsSource": "AtsIntegration::Greenhouse::Listing"}, {"autoPosted": True}],
)
def test_ats_imports_are_skipped_unless_asked_for(httpx_mock, overrides) -> None:
    page = _role_page(
        "software-engineer", startups=[("Acme", [_listing(1), _listing(2, **overrides)])]
    )
    httpx_mock.add_response(url=_role_url("software-engineer"), text=page, is_reusable=True)

    assert [job.ats_id for job in _scraper().fetch()] == ["1"]
    kept = _scraper(include_ats_imports=True).fetch()
    assert [job.ats_id for job in kept] == ["1", "2"]
    assert kept[1].raw is not None
    assert kept[1].raw.get("ats_source") or kept[1].raw.get("auto_posted")


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_role_url("software-engineer"), text=_role_page("software-engineer")
    )
    [job] = _scraper(include_descriptions=False).fetch()
    assert job.description is None


def test_remote_listing_without_an_office(httpx_mock) -> None:
    listing = _listing(
        1,
        locationNames=[],
        acceptedRemoteLocationNames=["United States", "United States"],
        remote=True,
        compensation=None,
        jobType="contract",
        yearsExperienceMin=None,
        liveStartAt=None,
    )
    httpx_mock.add_response(
        url=_role_url("software-engineer"),
        text=_role_page("software-engineer", startups=[("Acme", [listing])]),
    )

    [job] = _scraper().fetch()

    assert job.location == "United States"
    assert job.is_remote is True
    assert job.salary_currency is None
    assert job.salary_min is None
    assert job.employment_type == "CONTRACT"
    assert job.experience is None
    assert job.posted_at is None


def test_listings_without_identity_are_dropped(httpx_mock) -> None:
    listings = [_listing(1), _listing(2, title=" "), _listing(3, slug=None), _listing(4, id="x")]
    httpx_mock.add_response(
        url=_role_url("software-engineer"),
        text=_role_page("software-engineer", startups=[("Acme", listings), ("", [_listing(5)])]),
    )
    assert [job.ats_id for job in _scraper().fetch()] == ["1"]


def test_a_missing_role_is_skipped(httpx_mock) -> None:
    httpx_mock.add_response(url=_role_url("gone-role"), status_code=404)
    httpx_mock.add_response(
        url=_role_url("software-engineer"), text=_role_page("software-engineer")
    )
    assert [job.ats_id for job in _scraper("gone-role", "software-engineer").fetch()] == ["1"]


def test_all_roles_missing_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=_ROLE_RE, status_code=404, is_reusable=True)
    with pytest.raises(ScraperError, match="none of the role pages"):
        _scraper("a-role", "b-role").fetch()


def test_no_roles_returns_nothing() -> None:
    assert WellfoundScraper("any", role_slugs=()).fetch() == []


def test_blocked_page_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=_ROLE_RE, status_code=403, is_reusable=True)
    with pytest.raises(ScraperError, match="403"):
        _scraper().fetch()


@pytest.mark.parametrize(
    "page",
    [
        "<html>Just a moment...</html>",
        '<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>',
        _role_page("another-role"),
    ],
)
def test_page_without_job_data_is_an_error(httpx_mock, page: str) -> None:
    httpx_mock.add_response(url=_role_url("software-engineer"), text=page)
    with pytest.raises(ScraperError, match="no job"):
        _scraper().fetch()


def test_environment_firecrawl_key_is_not_used(httpx_mock, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-env")
    httpx_mock.add_response(
        url=_role_url("software-engineer"), text=_role_page("software-engineer")
    )
    assert len(_scraper().fetch()) == 1


def test_firecrawl_key_routes_pages_through_firecrawl(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_FIRECRAWL_URL,
        method="POST",
        json={
            "success": True,
            "data": {
                "rawHtml": _role_page("software-engineer"),
                "metadata": {"statusCode": 200},
            },
        },
    )

    [job] = _scraper(firecrawl_api_key="fc-key").fetch()

    assert job.ats_id == "1"
    request = httpx_mock.get_request()
    assert request.headers["Authorization"] == "Bearer fc-key"
    assert json.loads(request.content) == {
        "url": "https://wellfound.com/role/software-engineer",
        "formats": ["rawHtml"],
    }


def test_firecrawl_page_past_the_last_one_ends_the_role(httpx_mock) -> None:
    def firecrawl(page: int, page_count: int) -> dict[str, Any]:
        html = _role_page("software-engineer", page, page_count=page_count)
        return {"success": True, "data": {"rawHtml": html, "metadata": {"statusCode": 200}}}

    httpx_mock.add_response(url=_FIRECRAWL_URL, method="POST", json=firecrawl(1, 2))
    # Firecrawl follows the redirect and returns the first page.
    httpx_mock.add_response(url=_FIRECRAWL_URL, method="POST", json=firecrawl(1, 1))

    jobs = _scraper(firecrawl_api_key="fc-key").fetch()

    assert [job.ats_id for job in jobs] == ["1"]


def test_firecrawl_without_credits_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_FIRECRAWL_URL,
        method="POST",
        status_code=402,
        json={"success": False, "code": "insufficient_credits"},
    )
    with pytest.raises(ScraperError, match="Firecrawl returned 402"):
        _scraper(firecrawl_api_key="fc-key").fetch()


def test_get_description_reads_the_job_page(httpx_mock) -> None:
    posting = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        "title": "Engineer",
        "description": "<h3>About</h3><p>Build things.</p><ul><li>Ship</li></ul>",
    }
    httpx_mock.add_response(
        url="https://wellfound.com/jobs/1-engineer",
        text=f'<script type="application/ld+json">{json.dumps(posting)}</script>',
    )
    job = Job(
        url="https://wellfound.com/jobs/1-engineer",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.WELLFOUND,
        ats_id="1",
    )
    assert _scraper().get_description(job) == "About\nBuild things.\nShip"


@pytest.mark.parametrize(
    ("compensation", "expected"),
    [
        ("$120k – $160k", ("USD", 120_000, 160_000)),
        ("$90k – $120k CAD • No equity", ("CAD", 90_000, 120_000)),
        ("₹10L – ₹25L", ("INR", 1_000_000, 2_500_000)),
        ("₹5,00,000 – ₹8,00,000 • No equity", ("INR", 500_000, 800_000)),
        ("€50k – €70k", ("EUR", 50_000, 70_000)),
        ("£45k", ("GBP", 45_000, 45_000)),
        ("Up to $150k", ("USD", None, 150_000)),
        ("No equity", (None, None, None)),
        ("0.5% – 1.0%", (None, None, None)),
        (None, (None, None, None)),
    ],
)
def test_parse_salary(compensation: str | None, expected: tuple) -> None:
    assert _parse_salary(compensation) == expected


def test_get_description_of_a_failing_posting_is_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://wellfound.com/jobs/2-designer", status_code=403, is_reusable=True,
    )
    job = Job(
        url="https://wellfound.com/jobs/2-designer",
        title="Designer",
        company="Acme",
        ats_type=ATSType.WELLFOUND,
        ats_id="2",
    )
    assert _scraper().get_description(job) is None
