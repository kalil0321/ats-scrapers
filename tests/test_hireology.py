from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import HireologyScraper
from ats_scrapers.scrapers.base import ScraperRegistry

TENANT = "andersonautogroup"
CAREERS_URL = f"https://careers.hireology.com/{TENANT}/"
LISTING_URL = (
    f"https://api.hireology.com/v2/public/careers/{TENANT}"
    "?page=1&page_size=1000"
)


def _portal(
    *,
    tenant: str = TENANT,
    token: str = "header.payload.signature",
) -> str:
    return f"""
    <html>
      <head><title>Jobs for Anderson Auto Group</title></head>
      <body>
        <script>
          var startingData = {{
            "apiUrl": "https://api.hireology.com/v2",
            "apiToken": "{token}",
            "careersPath": "{tenant}"
          }};
        </script>
      </body>
    </html>
    """


def _job(job_id: int = 2_827_500) -> dict[str, object]:
    return {
        "id": job_id,
        "name": "Automotive Technician",
        "created_at": "2026-07-27T19:10:31.370Z",
        "status": "Open",
        "employment_status": "Full Time - hourly",
        "job_description": (
            "<h2>Role</h2><p>Diagnose and repair customer vehicles.</p>"
        ),
        "locations": [
            {
                "city": "Lincoln",
                "state": "NE",
                "zip_code": "68521",
                "address": "2500 Wildcat Drive",
            }
        ],
        "remote": False,
        "blind_posted": False,
        "job_family": {
            "id": 9,
            "name": "Service",
        },
        "career_site_url": (
            f"https://careers.hireology.com/{TENANT}/{job_id}/description"
        ),
        "application_path": f"/careers/{job_id}/application",
        "application_basic": True,
        "organization": {
            "id": 21_165,
            "name": "Anderson Ford of Lincoln",
            "type": "Location",
        },
        "compensation": {
            "is_comp_range": True,
            "comp_single_amount": "0.0",
            "comp_range_min": "25.0",
            "comp_range_max": "35.5",
            "comp_period": "hour",
            "comp_frequency": "weekly",
        },
    }


def _listing(
    jobs: list[dict[str, object]],
    *,
    count: int | None = None,
    page: int = 1,
) -> dict[str, object]:
    return {
        "data": jobs,
        "count": len(jobs) if count is None else count,
        "page": page,
        "page_size": 1_000,
    }


def _mock_listing(
    httpx_mock,
    jobs: list[dict[str, object]],
) -> None:
    httpx_mock.add_response(url=CAREERS_URL, text=_portal())
    httpx_mock.add_response(url=LISTING_URL, json=_listing(jobs))


def test_registry_resolves_hireology() -> None:
    assert ScraperRegistry.get(ATSType.HIREOLOGY) is HireologyScraper


def test_accepts_safe_path_punctuation() -> None:
    scraper = HireologyScraper("thelodgenursing&rehabcenter")

    assert scraper.company_slug == "thelodgenursing&rehabcenter"


def test_fetches_structured_public_jobs(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])

    before_fetch = datetime.now(UTC)
    job = HireologyScraper(TENANT).fetch()[0]
    after_fetch = datetime.now(UTC)

    assert job.ats_type is ATSType.HIREOLOGY
    assert job.ats_id == "2827500"
    assert job.title == "Automotive Technician"
    assert job.company == "Anderson Ford of Lincoln"
    assert str(job.url) == (
        "https://careers.hireology.com/"
        "andersonautogroup/2827500/description"
    )
    assert str(job.apply_url) == str(job.url)
    assert job.location == "2500 Wildcat Drive, Lincoln, NE, 68521"
    assert job.country_iso == "US"
    assert job.region == "North America"
    assert job.is_remote is False
    assert job.salary_currency == "USD"
    assert job.salary_period == "HOUR"
    assert job.salary_min == 25
    assert job.salary_max == 35.5
    assert job.salary_summary == "25 - 35.5 per hour"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Full Time - hourly"
    assert job.department == "Service"
    assert job.requisition_id == "2827500"
    assert job.description == "Role\nDiagnose and repair customer vehicles."
    assert job.posted_at == datetime(
        2026,
        7,
        27,
        19,
        10,
        31,
        370_000,
        tzinfo=UTC,
    )
    assert job.language is None
    assert job.fetched_at is not None
    assert before_fetch <= job.fetched_at <= after_fetch
    assert job.raw == {
        "organization_id": 21_165,
        "organization_type": "Location",
        "job_family_id": 9,
        "application_path": "/careers/2827500/application",
        "application_basic": True,
        "blind_posted": False,
        "compensation_frequency": "weekly",
    }


def test_listing_only_mode_skips_description_and_uses_fallback_company(
    httpx_mock,
) -> None:
    item = _job()
    item["organization"] = {}
    _mock_listing(httpx_mock, [item])

    job = HireologyScraper(
        TENANT,
        include_descriptions=False,
        company_name="Anderson Auto Group",
    ).fetch()[0]

    assert job.company == "Anderson Auto Group"
    assert job.description is None


def test_remote_canadian_single_salary_maps_geography(httpx_mock) -> None:
    item = _job()
    item.update(
        locations=[{"city": "Toronto", "state": "ON", "zip_code": "M5V"}],
        remote=True,
        employment_status="Contract",
        compensation={
            "is_comp_range": False,
            "comp_single_amount": "95",
            "comp_range_min": "0",
            "comp_range_max": "0",
            "comp_period": "hour",
            "comp_frequency": "biweekly",
        },
    )
    _mock_listing(httpx_mock, [item])

    job = HireologyScraper(TENANT).fetch()[0]

    assert job.country_iso == "CA"
    assert job.region == "North America"
    assert job.is_remote is True
    assert job.salary_currency == "CAD"
    assert job.salary_min == 95
    assert job.salary_max == 95
    assert job.salary_summary == "95 per hour"
    assert job.employment_type == "CONTRACT"


def test_absent_compensation_does_not_emit_empty_salary(httpx_mock) -> None:
    item = _job()
    item["compensation"] = {
        "is_comp_range": True,
        "comp_range_min": "0",
        "comp_range_max": "0",
        "comp_period": "hour",
    }
    _mock_listing(httpx_mock, [item])

    job = HireologyScraper(TENANT).fetch()[0]

    assert job.salary is None
    assert job.salary_currency is None
    assert job.salary_period is None
    assert job.salary_summary is None
    assert job.salary_min is None
    assert job.salary_max is None


def test_empty_active_listing_returns_empty(httpx_mock) -> None:
    _mock_listing(httpx_mock, [])

    assert HireologyScraper(TENANT).fetch() == []


def test_paginates_and_reconciles_total(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(
        "ats_scrapers.scrapers.hireology.PAGE_SIZE",
        1,
    )
    first = _job()
    second = _job(2_827_501)
    second["name"] = "Service Advisor"
    httpx_mock.add_response(url=CAREERS_URL, text=_portal())
    httpx_mock.add_response(
        url=(
            f"https://api.hireology.com/v2/public/careers/{TENANT}"
            "?page=1&page_size=1"
        ),
        json=_listing([first], count=2),
    )
    httpx_mock.add_response(
        url=(
            f"https://api.hireology.com/v2/public/careers/{TENANT}"
            "?page=2&page_size=1"
        ),
        json=_listing([second], count=2, page=2),
    )

    jobs = HireologyScraper(TENANT).fetch()

    assert [job.requisition_id for job in jobs] == ["2827500", "2827501"]


def test_duplicate_job_ids_fail_closed(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job(), _job()])

    with pytest.raises(ScraperError, match="duplicate job id"):
        HireologyScraper(TENANT).fetch()


def test_matching_display_fields_preserve_distinct_requisitions(
    httpx_mock,
) -> None:
    older = _job()
    older["created_at"] = "2026-07-01T10:00:00Z"
    newer = _job(2_827_501)
    newer["created_at"] = "2026-07-29T10:00:00Z"
    _mock_listing(httpx_mock, [older, newer])

    jobs = HireologyScraper(TENANT).fetch()

    assert [job.requisition_id for job in jobs] == [
        "2827500",
        "2827501",
    ]


def test_non_open_job_fails_closed(httpx_mock) -> None:
    item = _job()
    item["status"] = "Closed"
    _mock_listing(httpx_mock, [item])

    with pytest.raises(ScraperError, match="non-open job"):
        HireologyScraper(TENANT).fetch()


def test_unexpected_job_url_fails_closed(httpx_mock) -> None:
    item = _job()
    item["career_site_url"] = (
        "https://evil.example.com/another-tenant/2827500/description"
    )
    _mock_listing(httpx_mock, [item])

    with pytest.raises(ScraperError, match="unexpected URL"):
        HireologyScraper(TENANT).fetch()


def test_parent_portal_accepts_valid_child_location_url(httpx_mock) -> None:
    item = _job()
    item["career_site_url"] = (
        "https://careers.hireology.com/"
        "andersonfordofstjoe/2827500/description"
    )
    _mock_listing(httpx_mock, [item])

    job = HireologyScraper(TENANT).fetch()[0]

    assert str(job.url) == (
        "https://careers.hireology.com/"
        "andersonfordofstjoe/2827500/description"
    )


def test_child_location_url_safely_reencodes_embedded_slash(httpx_mock) -> None:
    item = _job()
    item["career_site_url"] = (
        "https://careers.hireology.com/"
        "brandthamptoninn&suitesprovidence%2Fsmithfield/"
        "2827500/description"
    )
    _mock_listing(httpx_mock, [item])

    job = HireologyScraper(TENANT).fetch()[0]

    assert str(job.url) == (
        "https://careers.hireology.com/"
        "brandthamptoninn%26suitesprovidence%2Fsmithfield/"
        "2827500/description"
    )


def test_missing_public_configuration_maps_to_company_not_found(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        url=CAREERS_URL,
        text="<html><title>Hireology</title></html>",
    )

    with pytest.raises(CompanyNotFoundError):
        HireologyScraper(TENANT).fetch()


def test_configuration_tenant_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=CAREERS_URL,
        text=_portal(tenant="another-tenant"),
    )

    with pytest.raises(ScraperError, match="identified"):
        HireologyScraper(TENANT).fetch()


def test_missing_anonymous_token_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=CAREERS_URL,
        text=_portal(token="not-a-jwt"),
    )

    with pytest.raises(ScraperError, match="anonymous API token"):
        HireologyScraper(TENANT).fetch()


@pytest.mark.parametrize(
    "slug",
    [
        "../other",
        "tenant/other",
        "tenant?other",
        "tenant#other",
        "tenant\nother",
    ],
)
def test_rejects_unsafe_path_slugs(slug: str) -> None:
    with pytest.raises(ScraperError, match="one public URL path segment"):
        HireologyScraper(slug)
