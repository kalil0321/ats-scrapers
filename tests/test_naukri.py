"""Tests for Naukri India."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import NaukriScraper, ScraperRegistry
from jobhive.scrapers.naukri import _normalise_proxy_url


def _job(job_id: str = "081125000082") -> dict:
    return {
        "jobId": job_id,
        "title": "AI Engineer/Data Scientist",
        "companyName": "PwC Service Delivery Center",
        "jdURL": (
            "/job-listings-ai-engineer-data-scientist-pwc-service-delivery-center-"
            "hyderabad-pune-bengaluru-2-to-7-years-081125000082"
        ),
        "staticUrl": "pwc-service-delivery-center-jobs-careers-4394",
        "createdDate": 1778492463226,
        "footerPlaceholderLabel": "1 Day Ago",
        "tagsAndSkills": "Data Science,LLM,Python",
        "placeholders": [
            {"type": "experience", "label": "2-7 Yrs"},
            {"type": "salary", "label": "10-20 Lacs PA"},
            {"type": "location", "label": "Hyderabad, Pune, Bengaluru"},
        ],
        "currency": "INR",
        "experienceText": "2-7 Yrs",
        "jobDescription": "Build AI systems<br><br>Ship reliable models.",
        "companyId": 576625,
    }


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "body"

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.proxies: dict[str, str] = {}
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def test_registry_resolves_naukri() -> None:
    assert ScraperRegistry.get(ATSType.NAUKRI) is NaukriScraper


def test_parses_naukri_job_payload() -> None:
    scraper = NaukriScraper("any")
    job = scraper._parse_item(_job())
    assert job is not None
    assert job.ats_type is ATSType.NAUKRI
    assert job.ats_id == "081125000082"
    assert job.title == "AI Engineer/Data Scientist"
    assert job.company == "PwC Service Delivery Center"
    assert str(job.url).startswith("https://www.naukri.com/job-listings-ai-engineer")
    assert job.location == "Hyderabad, Pune, Bengaluru"
    assert job.country_iso == "IN"
    assert job.salary_currency == "INR"
    assert job.salary_min == 1000000
    assert job.salary_max == 2000000
    assert job.experience == 2
    assert job.description == "Build AI systems Ship reliable models."
    assert job.raw is not None
    assert job.raw["company_id"] == 576625
    assert job.raw["experience_label"] == "2-7 Yrs"


def test_fetch_paginates_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.naukri as n

    session = _Session(
        [
            _Response(200, {"noOfJobs": 40, "jobDetails": [_job("1"), _job("2")]}),
            _Response(200, {"noOfJobs": 40, "jobDetails": [_job("2"), _job("3")]}),
        ]
    )

    def make_session(impersonate: str) -> _Session:
        assert impersonate == "chrome136"
        return session

    monkeypatch.setattr(n, "cffi_requests", SimpleNamespace(Session=make_session))
    monkeypatch.setattr(n, "_generate_nkparam", lambda: "token")

    jobs = NaukriScraper(
        "any",
        keywords=("python",),
        locations=("",),
        page_size=2,
        max_pages=2,
    ).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3"]
    assert session.calls[0]["params"]["keyword"] == "python"
    assert session.calls[0]["headers"]["nkparam"] == "token"


def test_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.naukri as n

    session = _Session([_Response(500)])

    def make_session(impersonate: str) -> _Session:
        return session

    monkeypatch.setattr(n, "cffi_requests", SimpleNamespace(Session=make_session))
    monkeypatch.setattr(n, "_generate_nkparam", lambda: "token")
    with pytest.raises(ScraperError):
        NaukriScraper("any", keywords=("python",), locations=("",), max_pages=1).fetch()


def test_normalises_host_port_user_pass_proxy() -> None:
    assert (
        _normalise_proxy_url("http://proxy.example:1000:user:pass")
        == "http://user:pass@proxy.example:1000"
    )
