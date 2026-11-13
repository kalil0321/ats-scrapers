"""Tests for the Darwinbox multi-tenant scraper."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC
from typing import Any

import pytest

import ats_scrapers.scrapers.darwinbox as darwinbox_module
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import DarwinboxScraper, ScraperRegistry

API = "https://airtel.darwinbox.in/ms/candidateapi/job/alljobs"


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}


@pytest.fixture
def fake_httpcloak(monkeypatch):
    responses: list[FakeResponse] = []
    calls: list[dict[str, Any]] = []
    module = types.ModuleType("httpcloak")

    def post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        if not responses:
            raise AssertionError("unexpected httpcloak POST")
        return responses.pop(0)

    module.post = post  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpcloak", module)
    return responses, calls


def listing(job_id: str = "a123", title: str = "Solution Architect") -> dict:
    return {
        "_id": "mongo-id",
        "id": job_id,
        "designation": "designation-id",
        "designation_display_name": title,
        "internal_job_code": "Job_123",
        "employee_type": "employee-type-id",
        "experience_from": "3",
        "experience_to": "7",
        "jd": "&lt;p&gt;Build reliable platforms.&lt;/p&gt;",
        "country": "India",
        "created_on": "2026-05-08T05:06:48.000Z",
        "posted_on": 1778178600,
        "is_remote": 1,
        "department_name": "Enterprise Sales",
        "emp_type_name": "Employee",
        "functional_area_name": "Airtel Core",
        "tool_tip_locations": [
            "Bangalore, Karnataka, India",
            "Mumbai, Maharashtra, India",
        ],
        "job_tags": ["priority"],
    }


def payload(items: list[dict], total: int | None = None) -> dict:
    return {
        "status": "success",
        "data": items,
        "job_counts": len(items) if total is None else total,
    }


def test_registry_resolves_darwinbox() -> None:
    assert ScraperRegistry.get(ATSType.DARWINBOX) is DarwinboxScraper
    assert DarwinboxScraper.fetch_engine == "cloak"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("airtel", ("airtel", "in")),
        ("pwc.com", ("pwc", "com")),
        ("bigbasket.in", ("bigbasket", "in")),
        (
            "https://sbchero.darwinbox.com/ms/candidate/careers",
            ("sbchero", "com"),
        ),
    ],
)
def test_resolves_supported_tenant_forms(value: str, expected: tuple[str, str]) -> None:
    assert DarwinboxScraper(value)._resolve_tenant() == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bad slug",
        "bad_slug",
        f"{'a' * 64}.in",
        "trailing-.com",
        "https://example.com/careers",
    ],
)
def test_rejects_invalid_tenant(value: str) -> None:
    with pytest.raises(ScraperError):
        DarwinboxScraper(value)._resolve_tenant()


def test_parses_current_alljobs_payload(fake_httpcloak) -> None:
    responses, calls = fake_httpcloak
    responses.append(FakeResponse(payload([listing()])))

    [result] = DarwinboxScraper("airtel").fetch()

    assert result.ats_type is ATSType.DARWINBOX
    assert result.ats_id == "a123"
    assert result.global_id == "darwinbox:a123"
    assert result.title == "Solution Architect"
    assert result.company == "airtel"
    assert result.location == (
        "Bangalore, Karnataka, India; Mumbai, Maharashtra, India"
    )
    assert result.country_iso == "IN"
    assert result.region == "Asia"
    assert result.is_remote is True
    assert result.department == "Enterprise Sales"
    assert result.team == "Airtel Core"
    assert result.requisition_id == "Job_123"
    assert result.employment_type == "FULL_TIME"
    assert result.commitment == "Employee"
    assert result.experience == 3
    assert result.description == "Build reliable platforms."
    assert result.posted_at is not None
    assert str(result.url) == "https://airtel.darwinbox.in/ms/candidate/careers/a123"
    assert calls[0]["url"] == API
    assert calls[0]["params"] == {"companyId": "main"}
    assert calls[0]["json"]["page"] == 1
    assert calls[0]["timeout"] == 30_000


def test_uses_catalog_company_name(fake_httpcloak) -> None:
    responses, _calls = fake_httpcloak
    responses.append(FakeResponse(payload([listing()])))

    [result] = DarwinboxScraper("airtel", company_name="Airtel").fetch()

    assert result.company == "Airtel"


def test_com_tenant_uses_com_host(fake_httpcloak) -> None:
    responses, calls = fake_httpcloak
    responses.append(FakeResponse(payload([])))
    assert DarwinboxScraper("pwc.com").fetch() == []
    assert calls[0]["url"].startswith("https://pwc.darwinbox.com/")


def test_paginates_and_deduplicates(fake_httpcloak) -> None:
    responses, calls = fake_httpcloak
    first = [listing(f"id-{index}", f"Job {index}") for index in range(10)]
    second = [listing("id-9", "Job 9"), listing("id-10", "Job 10")]
    responses.extend(
        [
            FakeResponse(payload(first, total=11)),
            FakeResponse(payload(second, total=11)),
        ]
    )

    jobs = DarwinboxScraper("airtel").fetch()

    assert len(jobs) == 11
    assert [call["json"]["page"] for call in calls] == [1, 2]


def test_raises_instead_of_silently_truncating_at_page_cap(fake_httpcloak, monkeypatch) -> None:
    responses, _calls = fake_httpcloak
    monkeypatch.setattr(darwinbox_module, "MAX_PAGES", 1)
    responses.append(
        FakeResponse(
            payload(
                [listing(f"id-{index}", f"Job {index}") for index in range(10)],
                total=20,
            )
        )
    )

    with pytest.raises(ScraperError, match="reached the safety cap"):
        DarwinboxScraper("airtel").fetch()


def test_preserves_zero_experience_and_normalizes_country_and_naive_time(
    fake_httpcloak,
) -> None:
    responses, _calls = fake_httpcloak
    item = listing()
    item.update(
        {
            "country": "Thailand",
            "experience_from": 0,
            "experience_from_num": 4,
            "posted_on": None,
            "created_on": "2026-05-08T05:06:48",
        }
    )
    responses.append(FakeResponse(payload([item])))

    [result] = DarwinboxScraper("airtel").fetch()

    assert result.country_iso == "TH"
    assert result.region == "Asia"
    assert result.experience == 0
    assert result.posted_at is not None
    assert result.posted_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("country", "country_iso", "region"),
    [
        ("Canada", "CA", "North America"),
        ("United Kingdom", "GB", "Europe"),
        ("United States", "US", "North America"),
    ],
)
def test_normalizes_global_countries(
    fake_httpcloak,
    country: str,
    country_iso: str,
    region: str,
) -> None:
    responses, _calls = fake_httpcloak
    item = listing()
    item["country"] = country
    responses.append(FakeResponse(payload([item])))

    [result] = DarwinboxScraper("airtel").fetch()

    assert result.country_iso == country_iso
    assert result.region == region


def test_skips_rows_missing_identity(fake_httpcloak) -> None:
    responses, _calls = fake_httpcloak
    responses.append(
        FakeResponse(payload([listing(job_id=""), listing(title="")]))
    )
    assert DarwinboxScraper("airtel").fetch() == []


def test_api_failure_envelope_raises(fake_httpcloak) -> None:
    responses, _calls = fake_httpcloak
    responses.append(FakeResponse({"status": "failure", "message": "boom"}))
    with pytest.raises(ScraperError, match="API failure"):
        DarwinboxScraper("airtel").fetch()


def test_non_object_payload_raises(fake_httpcloak) -> None:
    responses, _calls = fake_httpcloak
    responses.append(FakeResponse([]))
    with pytest.raises(ScraperError, match="non-object"):
        DarwinboxScraper("airtel").fetch()
