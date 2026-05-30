"""Tests for the NodeFlair (Singapore tech) scraper.

NodeFlair's listing API ships everything we need on the index endpoint;
no per-listing detail fetches needed. These tests pin:

1. JSON envelope parsing (``job_listings`` + ``total_listings_count``).
2. URL composition: clean ``nodeflair.com{job_path}`` with itm_* params
   stripped.
3. Country → ISO mapping.
4. ``is_salary_estimated=True`` suppresses the canonical salary fields.
5. Pagination fans out from page 1's total count.
6. Cloudflare-bypass headers (Chrome UA + Referer) are sent verbatim.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import NodeFlairScraper, ScraperRegistry, get_scraper

_API_RE = re.compile(r"^https://www\.nodeflair\.com/api/v2/jobs")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.nodeflair as nf
    monkeypatch.setattr(nf, "MAX_RETRIES", 1)
    monkeypatch.setattr(nf, "RETRY_BASE_DELAY", 0.0)


def _listing(
    *,
    job_id: int = 521828,
    title: str = "Senior Backend Engineer",
    company: str = "Acme Pte Ltd",
    company_id: int = 847,
    rating: float | None = 4.2,
    country: str = "Singapore",
    position: str = "Backend",
    seniority: list[str] | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    currency: str | None = None,
    frequency: str = "Monthly",
    is_estimated: bool = False,
    tech_stacks: list[dict] | None = None,
    time_ago: str = "about 1 day",
    job_path: str | None = None,
) -> dict:
    return {
        "id": job_id,
        "job_path": (
            job_path if job_path is not None
            else f"/jobs/acme-{title.lower().replace(' ', '-')}-{job_id}"
                 "?itm_campaign=job_search&itm_medium=listing"
                 "&itm_source=nodeflair_jobs"
        ),
        "position": position,
        "title": title,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "currency": currency,
        "remuneration_frequency": frequency,
        "tech_stacks": tech_stacks if tech_stacks is not None else [
            {"name": "Python"}, {"name": "Postgres"},
        ],
        "seniority": seniority if seniority is not None else ["Senior", "Lead"],
        "time_ago": time_ago,
        "company": {
            "id": company_id, "rating": rating, "companyname": company,
            "avatar": f"https://nodeflair.com/api/v2/companies/{company_id}.png",
        },
        "is_salary_estimated": is_estimated,
        "formatted_salary_min": "",
        "formatted_salary_max": "",
        "country": country,
    }


def _envelope(items: list[dict], total: int | None = None) -> dict:
    return {
        "job_listings": items,
        "total_listings_count": total if total is not None else len(items),
        "has_job_alert": False,
    }


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_nodeflair() -> None:
    assert ScraperRegistry.get(ATSType.NODEFLAIR) is NodeFlairScraper


def test_get_scraper_returns_nodeflair() -> None:
    s = get_scraper("nodeflair", "any")
    assert isinstance(s, NodeFlairScraper)


# --- Happy path -------------------------------------------------------------


def test_parses_basic_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing()]))
    jobs = NodeFlairScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_id == "521828"
    assert j.ats_type is ATSType.NODEFLAIR
    assert j.title == "Senior Backend Engineer"
    assert j.company == "Acme Pte Ltd"
    assert j.country_iso == "SG"
    assert j.region == "Asia"
    assert j.location == "Singapore"
    assert j.language == "en"
    assert j.department == "Backend"
    assert j.commitment == "Senior, Lead"


def test_url_strips_itm_query_params(httpx_mock) -> None:
    """``job_path`` ships with ``?itm_campaign=...`` tracking params.
    Strip them so the canonical URL is stable across renders / locales."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(
        job_path="/jobs/acme-senior-backend-engineer-521828"
                 "?itm_campaign=job_search&itm_medium=listing",
    )]))
    j = NodeFlairScraper("any").fetch()[0]
    assert "?" not in str(j.url)
    assert str(j.url).endswith("/jobs/acme-senior-backend-engineer-521828")


def test_url_fallback_when_job_path_missing(httpx_mock) -> None:
    listing = _listing()
    listing["job_path"] = ""
    httpx_mock.add_response(url=_API_RE, json=_envelope([listing]))
    j = NodeFlairScraper("any").fetch()[0]
    assert str(j.url) == "https://www.nodeflair.com/jobs/521828"


# --- Country & region -------------------------------------------------------


@pytest.mark.parametrize(
    ("country", "expected_iso"),
    [
        ("Singapore", "SG"),
        ("Malaysia", "MY"),
        ("Thailand", "TH"),
        ("Vietnam", "VN"),
        ("Indonesia", "ID"),
        ("Japan", "JP"),
        ("India", "IN"),
    ],
)
def test_country_iso_mapping(httpx_mock, country: str, expected_iso: str) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(country=country)]))
    j = NodeFlairScraper("any", country=None).fetch()[0]
    assert j.country_iso == expected_iso
    assert j.region == "Asia"


def test_unknown_country_leaves_iso_none(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(country="Mars")]))
    j = NodeFlairScraper("any", country=None).fetch()[0]
    assert j.country_iso is None
    assert j.region is None
    assert j.location == "Mars"


def test_australia_is_not_tagged_asia(httpx_mock) -> None:
    """AU/NZ are technically Oceania — they get country_iso but not
    region='Asia' even though NodeFlair lists them in its APAC feed."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(country="Australia")]))
    j = NodeFlairScraper("any", country=None).fetch()[0]
    assert j.country_iso == "AU"
    assert j.region is None


# --- Country filter wiring --------------------------------------------------


def test_country_filter_sent_as_array_param(httpx_mock) -> None:
    """Default ``country='Singapore'`` must become ``?countries[]=Singapore``
    on the wire — that's NodeFlair's accepted query shape."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing()]))
    NodeFlairScraper("any").fetch()
    req = httpx_mock.get_requests()[0]
    assert "countries%5B%5D=Singapore" in str(req.url) or "countries[]=Singapore" in str(req.url)


def test_country_none_omits_filter(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing()]))
    NodeFlairScraper("any", country=None).fetch()
    req = httpx_mock.get_requests()[0]
    assert "countries" not in str(req.url)


# --- Salary -----------------------------------------------------------------


def test_employer_supplied_salary_populates_canonical_fields(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(
        salary_min=6000, salary_max=9000, currency="SGD",
        frequency="Monthly", is_estimated=False,
    )]))
    j = NodeFlairScraper("any").fetch()[0]
    assert j.salary_min == 6000
    assert j.salary_max == 9000
    assert j.salary_currency == "SGD"
    assert j.salary_period == "MONTH"


def test_estimated_salary_is_suppressed_to_raw(httpx_mock) -> None:
    """``is_salary_estimated=True`` is NodeFlair's ML-derived range; we
    don't promote it to the canonical fields (mis-leading) but keep the
    value in ``raw`` for consumers that want it."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(
        salary_min=5000, salary_max=8000, currency="SGD",
        is_estimated=True,
    )]))
    j = NodeFlairScraper("any").fetch()[0]
    assert j.salary_min is None
    assert j.salary_max is None
    assert j.salary_currency is None
    assert j.salary_period is None
    assert j.raw is not None
    assert j.raw["estimated_salary"]["min"] == 5000


def test_salary_period_unknown_frequency_falls_through(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(
        salary_min=100, salary_max=200, currency="SGD",
        frequency="WhenTheStarsAlign",
    )]))
    j = NodeFlairScraper("any").fetch()[0]
    assert j.salary_period is None


# --- Employment type / seniority -------------------------------------------


def test_intern_seniority_maps_to_intern(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(
        seniority=["Intern"],
    )]))
    j = NodeFlairScraper("any").fetch()[0]
    assert j.employment_type == "INTERN"
    assert j.commitment == "Intern"


def test_non_intern_seniority_leaves_employment_type_none(httpx_mock) -> None:
    """Senior/Mid/Lead/Manager all imply FULL_TIME in practice but
    NodeFlair doesn't ship a structured employment-type field, so we
    don't guess — leave None for downstream enrichment to fill."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(
        seniority=["Senior", "Lead"],
    )]))
    j = NodeFlairScraper("any").fetch()[0]
    assert j.employment_type is None


# --- Raw overflow ----------------------------------------------------------


def test_raw_captures_tech_stack_and_rating(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing(
        tech_stacks=[{"name": "Go"}, {"name": "Kafka"}],
        rating=4.5,
    )]))
    j = NodeFlairScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw["tech_stacks"] == ["Go", "Kafka"]
    assert j.raw["company_rating"] == 4.5
    assert j.raw["company_id"] == 847


# --- Pagination -------------------------------------------------------------


def test_paginates_from_total_count(httpx_mock) -> None:
    """Page 1 ships ``total_listings_count``; the scraper computes
    ``pages = ceil(total / PER_PAGE)`` and fans out the rest in parallel."""
    page_one = _envelope(
        [_listing(job_id=i) for i in range(1, 13)], total=30,
    )
    page_two = _envelope(
        [_listing(job_id=i) for i in range(13, 25)], total=30,
    )
    page_three = _envelope(
        [_listing(job_id=i) for i in range(25, 31)], total=30,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.nodeflair\.com/api/v2/jobs.*page=1"),
        json=page_one,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.nodeflair\.com/api/v2/jobs.*page=2"),
        json=page_two,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.nodeflair\.com/api/v2/jobs.*page=3"),
        json=page_three,
    )
    jobs = NodeFlairScraper("any").fetch()
    assert len(jobs) == 30


def test_max_pages_caps_fanout(httpx_mock) -> None:
    """If the server claims 1000 pages but max_pages=2, only 2 page
    requests should be issued (probe + 1 fan-out)."""
    big = _envelope(
        [_listing(job_id=i) for i in range(1, 13)], total=10_000,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.nodeflair\.com/api/v2/jobs.*page=1"),
        json=big,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.nodeflair\.com/api/v2/jobs.*page=2"),
        json=_envelope(
            [_listing(job_id=i) for i in range(13, 25)], total=10_000,
        ),
    )
    # Page 3 must NOT be requested — httpx_mock errors if it is.
    jobs = NodeFlairScraper("any", max_pages=2).fetch()
    assert len(jobs) == 24


def test_dedupes_jobs_with_same_id(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _listing(job_id=99, title="A"),
        _listing(job_id=99, title="A duplicate"),
    ]))
    jobs = NodeFlairScraper("any").fetch()
    assert len(jobs) == 1


def test_skips_listings_without_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _listing(job_id=1, title=""),
        {"id": "", "title": "no id"},
        _listing(job_id=2, title="OK"),
    ]))
    jobs = NodeFlairScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["2"]


# --- Headers ----------------------------------------------------------------


def test_sends_chrome_user_agent_and_referer(httpx_mock) -> None:
    """Cloudflare blocks the default httpx UA; we must send a full Chrome
    UA + Referer. Removing either reintroduces the 403 challenge."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing()]))
    NodeFlairScraper("any").fetch()
    req = httpx_mock.get_requests()[0]
    ua = req.headers.get("User-Agent") or ""
    assert "Chrome/" in ua
    assert req.headers.get("Referer") == "https://www.nodeflair.com/jobs"


# --- Errors -----------------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        NodeFlairScraper("any").fetch()


def test_429_with_retry_after_is_honored(
    monkeypatch: pytest.MonkeyPatch, httpx_mock,
) -> None:
    import jobhive.scrapers.nodeflair as nf
    monkeypatch.setattr(nf, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=_API_RE, status_code=429, headers={"Retry-After": "7"},
    )
    httpx_mock.add_response(url=_API_RE, json=_envelope([_listing()]))
    NodeFlairScraper("any").fetch()
    assert 7.0 in sleeps


def test_malformed_json_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, text="<html>maintenance</html>")
    with pytest.raises(ScraperError, match="non-JSON"):
        NodeFlairScraper("any").fetch()


def test_network_error_raises(httpx_mock) -> None:
    httpx_mock.add_exception(
        httpx.ConnectError("DNS failed"), url=_API_RE, is_reusable=True,
    )
    with pytest.raises(ScraperError, match="DNS failed"):
        NodeFlairScraper("any").fetch()


# --- Defensive --------------------------------------------------------------


def test_handles_empty_listings(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([]))
    assert NodeFlairScraper("any").fetch() == []


def test_handles_missing_listings_key(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json={})
    assert NodeFlairScraper("any").fetch() == []


def test_company_name_falls_back_to_unknown(httpx_mock) -> None:
    listing = _listing()
    listing["company"] = {}
    httpx_mock.add_response(url=_API_RE, json=_envelope([listing]))
    j = NodeFlairScraper("any").fetch()[0]
    assert j.company == "Unknown"
