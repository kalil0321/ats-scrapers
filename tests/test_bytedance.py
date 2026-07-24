"""Tests for the ByteDance scraper.

Scope: parser behaviour against a fixed fixture mirroring the live
``/api/v1/public/supplier/search/job/posts`` response. The HTTP layer
is shared verbatim with the TikTok scraper (same ATSx/Throne backend)
and is not exercised here — those tests would re-cover identical code.
"""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.bytedance import (
    API_URL,
    PAGE_SIZE,
    BytedanceScraper,
    _compose_description,
    _extract_label,
    _extract_location,
    _map_recruit_type,
)

# A realistic single job_post_list entry — fields and nesting shape
# verified against the live endpoint on 2026-05-12.
_FIXTURE = {
    "id": "7607020417963968773",
    "code": "A72890A",
    "title": "Machine Learning Engineer Graduate (Recommendation) - 2026 Start",
    "description": "Team Introduction\nWe build recommendation systems.",
    "requirement": "Minimum Qualifications\n- PhD or Masters in CS",
    "recruit_type": {
        "id": "201",
        "name": "正式",
        "en_name": "Regular",
        "i18n_name": "正式",
    },
    "job_category": {
        "id": "6704215862603155720",
        "name": "算法",
        "en_name": "Algorithm",
        "i18n_name": "Algorithm",
    },
    "city_info": {
        "code": "CT_163",
        "location_type": 3,
        "name": "新加坡",
        "en_name": "Singapore",
        "i18n_name": "Singapore",
        "parent": {
            "code": "CT_SG",
            "name": "新加坡",
            "en_name": "Singapore",
            "i18n_name": "Singapore",
        },
    },
    "tag_list": None,
    "job_subject": {
        "id": "7100000000000000001",
        "name": "PhD Graduates- 2026 Start",
        "en_name": "PhD Graduates- 2026 Start",
        "i18n_name": "PhD Graduates- 2026 Start",
    },
    "vacancies": None,
    "department_info": None,
    "job_post_info": {
        "min_salary": None,
        "max_salary": None,
        "currency": None,
    },
    "process_type": None,
    "publish_time": 1715500000,
}


def test_parses_fixture_into_job() -> None:
    job = BytedanceScraper("bytedance")._parse_job(_FIXTURE)
    assert job.ats_id == "7607020417963968773"
    assert job.ats_type is ATSType.BYTEDANCE
    assert job.company == "ByteDance"
    assert job.title.startswith("Machine Learning Engineer Graduate")
    assert str(job.url) == "https://joinbytedance.com/search/7607020417963968773"
    assert job.global_id == "bytedance:7607020417963968773"
    # ``code`` becomes ``requisition_id``; ``ats_id`` keeps the numeric id.
    assert job.requisition_id == "A72890A"


def test_description_concatenates_description_and_requirement() -> None:
    job = BytedanceScraper("bytedance")._parse_job(_FIXTURE)
    assert job.description is not None
    assert "Team Introduction" in job.description
    assert "Minimum Qualifications" in job.description
    # Two-newline separator between the two source fields.
    assert "We build recommendation systems.\n\nMinimum Qualifications" in job.description


def test_employment_type_mapped_from_recruit_type() -> None:
    """``recruit_type.en_name == 'Regular'`` → FULL_TIME, label preserved."""
    job = BytedanceScraper("bytedance")._parse_job(_FIXTURE)
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Regular"


def test_intern_recruit_type_maps_to_intern() -> None:
    item = {**_FIXTURE, "recruit_type": {"en_name": "Intern", "name": "实习"}}
    job = BytedanceScraper("bytedance")._parse_job(item)
    assert job.employment_type == "INTERN"
    assert job.commitment == "Intern"


def test_department_and_team_from_category_and_subject() -> None:
    job = BytedanceScraper("bytedance")._parse_job(_FIXTURE)
    assert job.department == "Algorithm"
    assert job.team == "PhD Graduates- 2026 Start"


def test_team_suppressed_when_equal_to_department() -> None:
    item = {
        **_FIXTURE,
        "job_subject": {"en_name": "Algorithm", "name": "算法"},
    }
    job = BytedanceScraper("bytedance")._parse_job(item)
    assert job.team is None


def test_location_walks_city_info_parent_chain() -> None:
    job = BytedanceScraper("bytedance")._parse_job(_FIXTURE)
    # Singapore (city) + Singapore (country parent) — comma-joined.
    assert job.location == "Singapore, Singapore"


def test_location_handles_legacy_city_list_shape() -> None:
    item = {
        **_FIXTURE,
        "city_info": None,
        "city_list": [
            {"name": "東京", "en_name": "Tokyo"},
            {"name": "Osaka"},
            {"name": "Osaka"},
            None,
        ],
    }
    job = BytedanceScraper("bytedance")._parse_job(item)
    assert job.location == "Tokyo; Osaka"


def test_salary_fields_absent_when_missing() -> None:
    job = BytedanceScraper("bytedance")._parse_job(_FIXTURE)
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None
    assert job.salary_period is None
    assert job.salary is None


def test_salary_fields_parsed_when_present() -> None:
    item = {
        **_FIXTURE,
        "job_post_info": {
            "min_salary": 120000,
            "max_salary": 180000,
            "currency": "USD",
        },
    }
    job = BytedanceScraper("bytedance")._parse_job(item)
    assert job.salary_min == 120000.0
    assert job.salary_max == 180000.0
    assert job.salary_currency == "USD"
    assert job.salary_period == "YEAR"


def test_raw_keeps_only_truthy_fields() -> None:
    job = BytedanceScraper("bytedance")._parse_job(_FIXTURE)
    assert job.raw is not None
    # ``tag_list``, ``department_info``, ``process_type`` are null → omitted.
    assert "tag_list" not in job.raw
    assert "department_info" not in job.raw
    assert "process_type" not in job.raw
    # ``job_category`` / ``job_subject`` / ``recruit_type`` survive.
    assert "job_category" in job.raw
    assert "job_subject" in job.raw
    assert "recruit_type" in job.raw


def test_ats_type_registered_for_bytedance() -> None:
    """The decorator wires the scraper into the registry."""
    from ats_scrapers.scrapers.base import ScraperRegistry

    assert ScraperRegistry.get(ATSType.BYTEDANCE) is BytedanceScraper


def test_fetch_retries_transient_response(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.bytedance as module

    monkeypatch.setattr(module, "MAX_RETRIES", 2)
    httpx_mock.add_response(url=API_URL, status_code=503)
    httpx_mock.add_response(
        url=API_URL,
        json={
            "code": 0,
            "data": {"job_post_list": [_FIXTURE], "count": 1},
        },
    )
    assert [job.ats_id for job in BytedanceScraper("any").fetch()] == [
        "7607020417963968773"
    ]


@pytest.mark.parametrize("status", [408, 429, 503])
def test_fetch_retries_transient_statuses(httpx_mock, monkeypatch, status) -> None:
    import ats_scrapers.fetch as fetch_module

    delays = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(
        "ats_scrapers.scrapers.bytedance.MAX_RETRIES", 2,
    )
    monkeypatch.setattr(fetch_module.asyncio, "sleep", fake_sleep)
    httpx_mock.add_response(
        url=API_URL,
        status_code=status,
        headers={"Retry-After": "1"},
    )
    httpx_mock.add_response(
        url=API_URL,
        json={
            "code": 0,
            "data": {"job_post_list": [_FIXTURE], "count": 1},
        },
    )

    assert len(BytedanceScraper("any").fetch()) == 1
    assert delays == [1.0]


def test_fetch_rejects_non_object_json(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=[])
    with pytest.raises(ScraperError, match="non-object"):
        BytedanceScraper("any").fetch()


def test_fetcher_uses_explicit_proxy() -> None:
    fetcher = BytedanceScraper(
        "any", proxy="http://proxy.example:8080",
    ).make_fetcher()
    assert fetcher.proxy == "http://proxy.example:8080"


def test_fetcher_uses_proxy_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "ATS_SCRAPERS_PROXY", "http://env-proxy.example:8080",
    )
    fetcher = BytedanceScraper("any").make_fetcher()
    assert fetcher.proxy == "http://env-proxy.example:8080"


def test_fetch_rejects_empty_full_catalogue(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json={"code": 0, "data": {"job_post_list": [], "count": 0}},
    )

    with pytest.raises(ScraperError, match="returned no jobs"):
        BytedanceScraper("any").fetch()


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"job_post_list": []},
        {"job_post_list": [], "count": None},
    ],
)
def test_fetch_rejects_incomplete_result_envelope(httpx_mock, data) -> None:
    httpx_mock.add_response(url=API_URL, json={"code": 0, "data": data})

    with pytest.raises(ScraperError):
        BytedanceScraper("any").fetch()


def test_fetch_rejects_unparseable_job_row(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json={
            "code": 0,
            "data": {"job_post_list": [{"id": "missing-title"}], "count": 1},
        },
    )

    with pytest.raises(ScraperError, match="parse every returned job"):
        BytedanceScraper("any").fetch()


def test_fetch_rejects_short_page_before_reported_count(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json={
            "code": 0,
            "data": {"job_post_list": [_FIXTURE], "count": 2},
        },
    )

    with pytest.raises(ScraperError, match="short page before count"):
        BytedanceScraper("any").fetch()


def test_fetch_rejects_overlapping_pages_before_reported_count(
    httpx_mock,
) -> None:
    first_page = [
        {**_FIXTURE, "id": str(index)}
        for index in range(PAGE_SIZE)
    ]
    overlapping_page = [
        {**_FIXTURE, "id": str(index)}
        for index in range(PAGE_SIZE // 2, PAGE_SIZE + PAGE_SIZE // 2)
    ]
    total = PAGE_SIZE * 2
    httpx_mock.add_response(
        url=API_URL,
        json={
            "code": 0,
            "data": {"job_post_list": first_page, "count": total},
        },
    )
    httpx_mock.add_response(
        url=API_URL,
        json={
            "code": 0,
            "data": {"job_post_list": overlapping_page, "count": total},
        },
    )

    with pytest.raises(
        ScraperError,
        match=rf"{PAGE_SIZE + PAGE_SIZE // 2}/{total} unique jobs",
    ):
        BytedanceScraper("any").fetch()


# --- Helper-function units --------------------------------------------------


def test_compose_description_collapses_blank_runs() -> None:
    out = _compose_description("Para one\n\n\n\nPara two", "Para three")
    assert out == "Para one\n\nPara two\n\nPara three"


def test_compose_description_returns_none_for_empty() -> None:
    assert _compose_description(None, "", "   ") is None


def test_compose_description_caps_at_25kb() -> None:
    big = "x" * 30_000
    out = _compose_description(big)
    assert out is not None
    assert len(out) == 25_000


def test_extract_label_prefers_en_name() -> None:
    assert _extract_label({"name": "算法", "en_name": "Algorithm"}) == "Algorithm"


def test_extract_label_falls_through_to_name() -> None:
    assert _extract_label({"name": "算法"}) == "算法"


def test_extract_label_returns_none_for_non_dict() -> None:
    assert _extract_label(None) is None
    assert _extract_label("Algorithm") is None


def test_map_recruit_type_unknown_label_kept_as_commitment() -> None:
    """Unknown labels fall through to ``commitment`` with no enum."""
    emp, com = _map_recruit_type({"en_name": "Apprentice"})
    assert emp is None
    assert com == "Apprentice"


def test_map_recruit_type_missing_returns_none_pair() -> None:
    assert _map_recruit_type(None) == (None, None)


def test_extract_location_country_only_walks_parent() -> None:
    item = {
        "city_info": {
            "en_name": "Mountain View",
            "parent": {
                "en_name": "California",
                "parent": {"en_name": "United States"},
            },
        }
    }
    assert _extract_location(item) == "Mountain View, California, United States"


def test_extract_location_returns_none_when_absent() -> None:
    assert _extract_location({}) is None
