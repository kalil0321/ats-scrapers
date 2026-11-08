"""Tests for ``BaseScraper.get_details`` and the Workday implementation."""

from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ats_scrapers.exceptions import DetailFetchError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper
from ats_scrapers.scrapers.workday import WorkdayScraper

TENANT = "https://acme.wd5.myworkdayjobs.com/External"
JOB_URL = f"{TENANT}/job/Seattle-WA/Engineer_R123"
DETAIL_URL = (
    "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"
    "/job/Seattle-WA/Engineer_R123"
)


def _job(
    *,
    ats_type: ATSType = ATSType.WORKDAY,
    url: str = JOB_URL,
    description: str | None = None,
    location: str | None = "2 Locations",
    raw: dict[str, Any] | None = None,
) -> Job:
    return Job(
        url=url,
        title="Engineer",
        company="Acme",
        ats_type=ats_type,
        ats_id="R123",
        description=description,
        location=location,
        raw=raw,
        fetched_at=datetime.now(UTC),
    )


def _workday_job(**kwargs: Any) -> Job:
    kwargs.setdefault("raw", {"externalPath": "/job/Seattle-WA/Engineer_R123"})
    return _job(**kwargs)


def _detail_payload() -> dict[str, Any]:
    return {
        "jobPostingInfo": {
            "jobDescription": "<p>Build &amp; ship</p>",
            "location": "Seattle, WA",
            "additionalLocations": ["Redmond, WA"],
            "startDate": "2026-11-01",
            "endDate": "2026-12-01",
        }
    }


def _dummy_scraper(description: str | None) -> BaseScraper:
    class DummyScraper(BaseScraper):
        ats = ATSType.CUSTOM

        async def afetch(self) -> list[Job]:
            return []

        def get_description(self, job: Job) -> str | None:
            return description

    return DummyScraper("dummy")


# --- BaseScraper default -----------------------------------------------------


def test_base_get_details_default_wraps_get_description() -> None:
    job = _job(ats_type=ATSType.CUSTOM)

    result = _dummy_scraper("hello").get_details(job)

    assert result is not job
    assert result.description == "hello"
    assert job.description is None


def test_base_get_details_default_returns_job_unchanged_when_no_description() -> None:
    job = _job(ats_type=ATSType.CUSTOM)

    assert _dummy_scraper(None).get_details(job) is job


def test_base_get_details_default_does_not_mutate_input_when_hook_mutates() -> None:
    class MutatingScraper(BaseScraper):
        ats = ATSType.CUSTOM

        async def afetch(self) -> list[Job]:
            return []

        def get_description(self, job: Job) -> str | None:
            job.description = "filled"
            job.location = "Seattle, WA"
            return job.description

    job = _job(ats_type=ATSType.CUSTOM, description=None, location="2 Locations")

    result = MutatingScraper("dummy").get_details(job)

    assert job.description is None
    assert job.location == "2 Locations"
    assert result is not job
    assert result.description == "filled"
    assert result.location == "Seattle, WA"


def test_base_get_details_default_applies_refreshed_description() -> None:
    job = _job(ats_type=ATSType.CUSTOM, description="old")

    result = _dummy_scraper("new").get_details(job)

    assert result.description == "new"
    assert job.description == "old"


def test_base_get_details_default_truncates_long_description() -> None:
    job = _job(ats_type=ATSType.CUSTOM)

    result = _dummy_scraper("x" * 30_000).get_details(job)

    assert result.description is not None
    assert len(result.description) == 25_000


# --- Workday -----------------------------------------------------------------


def test_workday_get_details_merges_description_location_and_dates(httpx_mock) -> None:
    httpx_mock.add_response(url=DETAIL_URL, json=_detail_payload())
    job = _workday_job()

    result = WorkdayScraper(TENANT).get_details(job)

    assert result.description == "Build & ship"
    assert result.location == "Seattle, WA | Redmond, WA"
    assert result.raw is not None
    assert result.raw["startDate"] == "2026-11-01"
    assert result.raw["endDate"] == "2026-12-01"
    assert result.raw["externalPath"] == "/job/Seattle-WA/Engineer_R123"
    # Input untouched.
    assert job.description is None
    assert job.location == "2 Locations"
    assert job.raw == {"externalPath": "/job/Seattle-WA/Engineer_R123"}


def test_workday_get_details_fetches_even_when_description_present(httpx_mock) -> None:
    httpx_mock.add_response(url=DETAIL_URL, json=_detail_payload())
    job = _workday_job(description="Already known.")

    result = WorkdayScraper(TENANT).get_details(job)

    assert len(httpx_mock.get_requests()) == 1
    assert result.description == "Already known."
    assert result.raw is not None
    assert result.raw["startDate"] == "2026-11-01"
    assert result.raw["endDate"] == "2026-12-01"


@pytest.mark.parametrize("status", [404, 429])
def test_workday_get_details_raises_with_status(httpx_mock, status: int) -> None:
    httpx_mock.add_response(url=DETAIL_URL, status_code=status)

    with pytest.raises(DetailFetchError) as excinfo:
        WorkdayScraper(TENANT).get_details(_workday_job())

    assert excinfo.value.status_code == status
    assert excinfo.value.url == DETAIL_URL


def test_workday_get_details_raises_on_transport_error(httpx_mock) -> None:
    error = httpx.ConnectError("boom")
    httpx_mock.add_exception(error, url=DETAIL_URL)

    with pytest.raises(DetailFetchError) as excinfo:
        WorkdayScraper(TENANT).get_details(_workday_job())

    assert excinfo.value.status_code is None
    assert excinfo.value.url == DETAIL_URL
    assert excinfo.value.__cause__ is error


def test_workday_get_details_raises_on_non_json_200(httpx_mock) -> None:
    httpx_mock.add_response(
        url=DETAIL_URL,
        text="<html>maintenance</html>",
        headers={"Content-Type": "text/html"},
    )

    with pytest.raises(DetailFetchError) as excinfo:
        WorkdayScraper(TENANT).get_details(_workday_job())

    assert excinfo.value.status_code == 200
    assert "text/html" in str(excinfo.value)


def test_workday_get_details_raises_on_non_dict_json(httpx_mock) -> None:
    httpx_mock.add_response(url=DETAIL_URL, json=[1, 2])

    with pytest.raises(DetailFetchError) as excinfo:
        WorkdayScraper(TENANT).get_details(_workday_job())

    assert excinfo.value.status_code == 200
    assert excinfo.value.url == DETAIL_URL


def test_workday_get_details_raises_on_non_dict_job_posting_info(httpx_mock) -> None:
    httpx_mock.add_response(url=DETAIL_URL, json={"jobPostingInfo": "oops"})

    with pytest.raises(DetailFetchError) as excinfo:
        WorkdayScraper(TENANT).get_details(_workday_job())

    assert excinfo.value.status_code == 200
    assert excinfo.value.url == DETAIL_URL


def test_workday_get_details_raises_without_external_path(httpx_mock) -> None:
    job = _job(url=f"{TENANT}/details/Engineer_R123", raw=None)

    with pytest.raises(DetailFetchError) as excinfo:
        WorkdayScraper(TENANT).get_details(job)

    assert excinfo.value.status_code is None
    assert httpx_mock.get_requests() == []


def test_workday_get_description_returns_none_on_detail_failure(httpx_mock) -> None:
    httpx_mock.add_response(url=DETAIL_URL, status_code=404)

    assert WorkdayScraper(TENANT).get_description(_workday_job()) is None


def test_workday_get_description_returns_existing_description_without_request(
    httpx_mock,
) -> None:
    job = _workday_job(description="Already known.")

    assert WorkdayScraper(TENANT).get_description(job) == "Already known."
    assert httpx_mock.get_requests() == []


def test_detail_fetch_error_is_scraper_error() -> None:
    assert isinstance(DetailFetchError("x", status_code=404, url="u"), ScraperError)


def test_detail_fetch_error_round_trips_through_pickle() -> None:
    err = DetailFetchError("m", status_code=429, url="u")

    back = pickle.loads(pickle.dumps(err))

    assert isinstance(back, DetailFetchError)
    assert back.status_code == 429
    assert back.url == "u"
    assert str(back) == "m"


def test_detail_fetch_error_supports_copy() -> None:
    err = DetailFetchError("m", status_code=429, url="u")

    for clone in (copy.copy(err), copy.deepcopy(err)):
        assert clone.status_code == 429
        assert clone.url == "u"
