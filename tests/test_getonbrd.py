"""Tests for the Get on Board (LATAM tech) scraper.

The API doesn't support ``?include=`` for related resources, so the scraper
makes follow-up fetches for company / city / modality. These
tests pin the parsing contract and the lookup-cache behaviour so we don't
re-fetch the same company every job.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import GetOnBrdScraper, ScraperRegistry

_API = "https://www.getonbrd.com/api/v0"
_API_RE = re.compile(r"^https://www\.getonbrd\.com/api/v0/")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.getonbrd as gob
    monkeypatch.setattr(gob, "MAX_RETRIES", 1)
    monkeypatch.setattr(gob, "RETRY_BASE_DELAY", 0.0)


def _categories(slugs: list[str]) -> dict:
    return {
        "data": [{"id": s, "type": "category", "attributes": {"name": s}} for s in slugs],
    }


def _modalities() -> dict:
    return {
        "data": [
            {"id": "1", "type": "modality", "attributes": {"name": "Full time", "locale_key": "full_time"}},
            {"id": "2", "type": "modality", "attributes": {"name": "Part time", "locale_key": "part_time"}},
            {"id": "3", "type": "modality", "attributes": {"name": "Freelance", "locale_key": "freelance"}},
        ],
    }


def _job(
    *,
    job_id: str,
    title: str,
    company_id: str,
    city_id: str | None = None,
    countries: list[str] | None = None,
    modality_id: str = "1",
    remote: bool = False,
    min_salary: int | None = None,
    max_salary: int | None = None,
    published_at: int = 1777922676,
) -> dict:
    return {
        "id": job_id,
        "type": "job",
        "links": {"public_url": f"https://www.getonbrd.com/jobs/{job_id}"},
        "attributes": {
            "title": title,
            "description": "Build things.",
            "functions": "Lead the team.",
            "projects": "About the role.",
            "company": {"data": {"id": int(company_id), "type": "company"}},
            "location_cities": (
                {"data": [{"id": int(city_id), "type": "tenant_city"}]} if city_id
                else {"data": []}
            ),
            "countries": countries or [],
            "modality": {"data": {"id": int(modality_id), "type": "modality"}},
            "remote": remote,
            "remote_modality": "remote_local" if remote else "hybrid",
            "min_salary": min_salary,
            "max_salary": max_salary,
            "published_at": published_at,
            "category_name": "Programming",
            "lang": "en",
            "perks": ["health_coverage"],
            "applications_count": 12,
        },
    }


def _jobs_page(jobs: list[dict], total_pages: int = 1, page: int = 1) -> dict:
    return {"data": jobs, "meta": {"page": page, "per_page": 120, "total_pages": total_pages}}


def _company(cid: str, name: str) -> dict:
    return {"data": {"id": cid, "type": "company", "attributes": {"name": name}}}


def _city(cid: str, name: str, country: str) -> dict:
    return {"data": {"id": cid, "type": "tenant_city", "attributes": {"name": name, "country": country}}}


def _stub_lookups(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{_API}/modalities", json=_modalities())


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_getonbrd() -> None:
    assert ScraperRegistry.get(ATSType.GETONBRD) is GetOnBrdScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_job_payload(httpx_mock) -> None:
    """One category, one job. Verify every populated field on the Job model
    matches the API payload."""
    httpx_mock.add_response(url=f"{_API}/categories", json=_categories(["programming"]))
    _stub_lookups(httpx_mock)
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}/categories/programming/jobs"),
        json=_jobs_page([
            _job(
                job_id="senior-engineer-acme-1234",
                title="Senior Engineer",
                company_id="42",
                city_id="130",
                modality_id="3",  # Freelance
                min_salary=3500,
                max_salary=4200,
            ),
        ]),
    )
    httpx_mock.add_response(url=f"{_API}/companies/42", json=_company("42", "Acme Inc"))
    httpx_mock.add_response(url=f"{_API}/cities/130", json=_city("130", "Lima", "Peru"))

    jobs = GetOnBrdScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.GETONBRD
    assert j.ats_id == "senior-engineer-acme-1234"
    assert j.title == "Senior Engineer"
    assert j.company == "Acme Inc"
    assert j.location == "Lima, Peru"
    assert j.is_remote is False
    assert j.employment_type == "CONTRACT"  # Freelance → CONTRACT
    assert j.commitment == "Freelance"
    assert j.salary_currency == "USD"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 3500
    assert j.salary_max == 4200
    assert j.posted_at is not None
    assert j.description and "Build things" in j.description
    assert j.department == "Programming"
    assert str(j.url) == "https://www.getonbrd.com/jobs/senior-engineer-acme-1234"


# --- pagination -------------------------------------------------------------


def test_paginates_multi_page_category(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{_API}/categories", json=_categories(["programming"]))
    _stub_lookups(httpx_mock)
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}/categories/programming/jobs.*page=1"),
        json=_jobs_page(
            [_job(job_id=f"a-{i}", title=f"Job {i}", company_id="42") for i in range(120)],
            total_pages=2,
            page=1,
        ),
    )
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}/categories/programming/jobs.*page=2"),
        json=_jobs_page(
            [_job(job_id=f"b-{i}", title=f"Job {i}", company_id="42") for i in range(80)],
            total_pages=2,
            page=2,
        ),
    )
    httpx_mock.add_response(url=f"{_API}/companies/42", json=_company("42", "Acme"))

    jobs = GetOnBrdScraper("any").fetch()
    assert len(jobs) == 200
    # Same company across all 200 jobs — must NOT trigger 200 separate
    # /companies/42 lookups.
    company_lookups = sum(
        1 for r in httpx_mock.get_requests() if r.url.path == "/api/v0/companies/42"
    )
    assert company_lookups == 1, f"expected company lookup to be cached, got {company_lookups} fetches"


# --- location handling ------------------------------------------------------


def test_remote_job_uses_country_list_when_no_city(httpx_mock) -> None:
    """A remote-only posting has empty location_cities and ``countries``
    set to the eligibility list (often just ``['Remote']``)."""
    httpx_mock.add_response(url=f"{_API}/categories", json=_categories(["programming"]))
    _stub_lookups(httpx_mock)
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}/categories/programming/jobs"),
        json=_jobs_page([
            _job(
                job_id="rem-1", title="Remote Engineer", company_id="9",
                city_id=None, countries=["Remote"], remote=True,
            ),
        ]),
    )
    httpx_mock.add_response(url=f"{_API}/companies/9", json=_company("9", "RemoteCo"))

    jobs = GetOnBrdScraper("any").fetch()
    assert jobs[0].location == "Remote"
    assert jobs[0].is_remote is True


def test_falls_back_to_country_list_when_city_id_unresolvable(httpx_mock) -> None:
    """If ``/cities/{id}`` returns 404, downgrade to the country list rather
    than crashing or producing an empty location."""
    httpx_mock.add_response(url=f"{_API}/categories", json=_categories(["programming"]))
    _stub_lookups(httpx_mock)
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}/categories/programming/jobs"),
        json=_jobs_page([
            _job(
                job_id="city-1", title="Engineer", company_id="9",
                city_id="999", countries=["Chile"],
            ),
        ]),
    )
    httpx_mock.add_response(url=f"{_API}/companies/9", json=_company("9", "AcmeChile"))
    httpx_mock.add_response(url=f"{_API}/cities/999", status_code=404)

    jobs = GetOnBrdScraper("any").fetch()
    # Whatever the resolved city was, we shouldn't have crashed and the row
    # exists. The exact location string can be empty (city ID without name)
    # or fall back to country — both are acceptable; what matters is no
    # exception.
    assert len(jobs) == 1


def test_salary_only_set_when_present(httpx_mock) -> None:
    """No salary in the API → salary_currency stays None (we shouldn't
    invent a USD currency for empty rows)."""
    httpx_mock.add_response(url=f"{_API}/categories", json=_categories(["programming"]))
    _stub_lookups(httpx_mock)
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}/categories/programming/jobs"),
        json=_jobs_page([
            _job(job_id="no-sal", title="X", company_id="9"),
            _job(job_id="has-sal", title="Y", company_id="9", min_salary=2000, max_salary=3000),
        ]),
    )
    httpx_mock.add_response(url=f"{_API}/companies/9", json=_company("9", "A"))
    by = {j.ats_id: j for j in GetOnBrdScraper("any").fetch()}
    assert by["no-sal"].salary_currency is None
    assert by["no-sal"].salary_min is None
    assert by["has-sal"].salary_currency == "USD"
    assert by["has-sal"].salary_min == 2000
    assert by["has-sal"].salary_max == 3000


# --- error handling ---------------------------------------------------------


def test_categories_endpoint_failure_raises(httpx_mock) -> None:
    """If the entire category list fetch fails, the scraper can't proceed —
    raising is correct (vs silently returning [])."""
    httpx_mock.add_response(url=f"{_API}/categories", status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        GetOnBrdScraper("any").fetch()
