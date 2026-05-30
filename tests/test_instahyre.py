"""Tests for Instahyre."""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import InstahyreScraper, ScraperRegistry
from jobhive.scrapers.instahyre import _compose_description

_API_LIST_RE = re.compile(
    r"^https://www\.instahyre\.com/api/v1/job_search(\?|$)"
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.instahyre as i

    monkeypatch.setattr(i, "MAX_RETRIES", 1)
    monkeypatch.setattr(i, "RETRY_BASE_DELAY", 0.0)


def _detail_url(job_id: int) -> str:
    return f"https://www.instahyre.com/api/v1/job_search/{job_id}"


def _job(job_id: int, title: str = "Frontend Developer") -> dict:
    return {
        "id": job_id,
        "title": title,
        "public_url": f"https://www.instahyre.com/job-{job_id}-frontend-developer/",
        "locations": "Bangalore,Gurgaon",
        "employer": {
            "id": 53522,
            "company_name": "Echos",
            "company_tagline": "Where deep tech meets real-world impact",
            "company_founded": 2025,
            "employee_count": 10,
            "instahyre_note": "Deep-tech engineering company.",
        },
        "keywords": ["React.js", "JavaScript", "Next.js"],
        "accept_outstation": True,
    }


def test_registry_resolves_instahyre() -> None:
    assert ScraperRegistry.get(ATSType.INSTAHYRE) is InstahyreScraper


def test_parses_instahyre_job_payload(httpx_mock) -> None:
    payload = _job(423594)
    httpx_mock.add_response(url=_API_LIST_RE, json={"objects": [payload]})
    httpx_mock.add_response(url=_detail_url(423594), json=payload)

    jobs = InstahyreScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.INSTAHYRE
    assert j.ats_id == "423594"
    assert j.title == "Frontend Developer"
    assert j.company == "Echos"
    assert j.location == "Bangalore, Gurgaon"
    assert j.description == _compose_description(payload)
    assert j.raw is not None
    assert j.raw["keywords"] == ["React.js", "JavaScript", "Next.js"]
    assert j.raw["employer_id"] == 53522


def test_paginates_until_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_LIST_RE, json={"objects": [_job(1), _job(2)]})
    httpx_mock.add_response(url=_API_LIST_RE, json={"objects": [_job(3)]})
    for jid in (1, 2, 3):
        httpx_mock.add_response(url=_detail_url(jid), json=_job(jid))

    jobs = InstahyreScraper("any", page_size=2).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]


def test_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_LIST_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        InstahyreScraper("any").fetch()


def test_detail_fetch_failure_keeps_list_description(httpx_mock) -> None:
    payload = _job(501)
    httpx_mock.add_response(url=_API_LIST_RE, json={"objects": [payload]})
    httpx_mock.add_response(url=_detail_url(501), status_code=503)

    jobs = InstahyreScraper("any").fetch()
    assert len(jobs) == 1
    assert jobs[0].description == _compose_description(payload)


def test_detail_wrong_id_skipped(httpx_mock) -> None:
    payload = _job(601)
    httpx_mock.add_response(url=_API_LIST_RE, json={"objects": [payload]})
    httpx_mock.add_response(url=_detail_url(601), json=_job(999))

    jobs = InstahyreScraper("any").fetch()
    assert jobs[0].description == _compose_description(payload)


def test_job_search_top_level_array_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_LIST_RE, json=["not", "an", "object"])
    with pytest.raises(ScraperError, match="not an object"):
        InstahyreScraper("any").fetch()
