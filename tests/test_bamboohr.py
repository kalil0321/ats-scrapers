"""Tests for the BambooHR scraper.

The scraper consumes BambooHR's public widget at `/jobs/embed2.php` —
server-rendered HTML grouped by department — and enriches each job
from `/careers/{id}/detail` (the SPA's hydration JSON) with
description, employment type, compensation, and posted date. Tests
pin:

1. Widget parsing (department→jobs association, location, URL forms)
2. Detail-API enrichment (description, employment type, etc.)
3. Retry behaviour (404 fail-fast, 429/5xx retry with backoff)
4. Whitespace + HTML entity handling
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import BambooHRScraper, ScraperRegistry, get_scraper

# --- module-level ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.bamboohr as bh
    monkeypatch.setattr(bh, "MAX_RETRIES", 1)
    monkeypatch.setattr(bh, "RETRY_BASE_DELAY", 0.0)


WIDGET_URL = "https://acme.bamboohr.com/jobs/embed2.php"

# The scraper always issues per-job ``/careers/{id}/detail`` calls after
# parsing the widget. Tests that don't care about enrichment leave those
# unmocked — pytestmark relaxes the unmatched-request check for the
# whole module so they don't have to enumerate every detail URL.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


def _widget_html(departments: list[dict[str, Any]]) -> str:
    """Build a BambooHR widget HTML response from a structured definition.

    Each department dict: {"id": int, "name": str, "jobs": [{...}]}
    Each job dict: {"id": int, "title": str, "location": str | None, "href": str | None}
    """
    parts = ['<div class="BambooHR-ATS-board"><h2>Open Positions</h2><ul class="BambooHR-ATS-Department-List">']
    for dept in departments:
        parts.append(
            f'<li id="bhrDepartmentID_{dept["id"]}" class="BambooHR-ATS-Department-Item">'
            f'<div id="department_{dept["id"]}" class="BambooHR-ATS-Department-Header">'
            f'{dept["name"]}'
            f'</div><ul class="BambooHR-ATS-Jobs-List">'
        )
        for job in dept["jobs"]:
            href = job.get("href") or f"//acme.bamboohr.com/careers/{job['id']}"
            loc_html = (
                f'<span class="BambooHR-ATS-Location">{job["location"]}</span>'
                if job.get("location") else ""
            )
            parts.append(
                f'<li id="bhrPositionID_{job["id"]}" class="BambooHR-ATS-Jobs-Item">'
                f'<a href="{href}">{job["title"]}</a>'
                f'{loc_html}'
                f'</li>'
            )
        parts.append('</ul></li>')
    parts.append('</ul></div>')
    return "".join(parts)


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_bamboohr() -> None:
    assert ScraperRegistry.get(ATSType.BAMBOOHR) is BambooHRScraper


def test_get_scraper_by_string_returns_bamboohr() -> None:
    s = get_scraper("bamboohr", "acme")
    assert isinstance(s, BambooHRScraper)
    assert s.company_slug == "acme"


# --- Widget parsing: happy path ---------------------------------------------


def test_parses_basic_widget(httpx_mock) -> None:
    html = _widget_html([
        {
            "id": 100,
            "name": "Engineering",
            "jobs": [
                {"id": 1, "title": "Backend Engineer", "location": "Berlin"},
                {"id": 2, "title": "Frontend Engineer", "location": "Remote"},
            ],
        }
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].location == "Berlin"
    assert jobs[0].department == "Engineering"
    assert jobs[0].ats_type is ATSType.BAMBOOHR
    assert jobs[0].company == "acme"


def test_returns_empty_for_widget_with_no_departments(httpx_mock) -> None:
    httpx_mock.add_response(url=WIDGET_URL, text='<div class="BambooHR-ATS-board"></div>')
    assert BambooHRScraper("acme").fetch() == []


def test_assigns_each_job_its_department(httpx_mock) -> None:
    """A job's `department` field must match the wrapping `<li>` block —
    not the most recent in document order, in case blocks are interleaved
    in unusual ways."""
    html = _widget_html([
        {"id": 1, "name": "Engineering", "jobs": [{"id": 10, "title": "Dev", "location": "X"}]},
        {"id": 2, "name": "Sales",       "jobs": [{"id": 20, "title": "AE",  "location": "Y"}]},
        {"id": 3, "name": "HR",          "jobs": [{"id": 30, "title": "RC",  "location": "Z"}]},
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    by_id = {j.ats_id: j.department for j in jobs}
    assert by_id == {"10": "Engineering", "20": "Sales", "30": "HR"}


def test_dedupes_jobs_with_same_id(httpx_mock) -> None:
    """Some tenants list the same job under multiple departments
    (cross-functional roles). Output must keep each job once."""
    html = _widget_html([
        {"id": 1, "name": "Engineering", "jobs": [{"id": 100, "title": "X", "location": "A"}]},
        {"id": 2, "name": "Product",     "jobs": [{"id": 100, "title": "X", "location": "A"}]},
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert len(jobs) == 1


def test_handles_missing_location(httpx_mock) -> None:
    html = _widget_html([
        {"id": 1, "name": "Eng", "jobs": [{"id": 10, "title": "X", "location": None}]},
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert jobs[0].location is None


def test_skips_position_without_anchor(httpx_mock) -> None:
    """Malformed widget entries (no <a> tag inside the position <li>) must
    be skipped, not raise."""
    bad_html = (
        '<li id="bhrDepartmentID_1" class="BambooHR-ATS-Department-Item">'
        '<div id="department_1" class="BambooHR-ATS-Department-Header">Eng</div>'
        '<ul class="BambooHR-ATS-Jobs-List">'
        '<li id="bhrPositionID_99" class="BambooHR-ATS-Jobs-Item">no anchor here</li>'
        '<li id="bhrPositionID_100" class="BambooHR-ATS-Jobs-Item">'
        '<a href="//acme.bamboohr.com/careers/100">Real Job</a></li>'
        '</ul></li>'
    )
    httpx_mock.add_response(url=WIDGET_URL, text=bad_html)
    jobs = BambooHRScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["100"]


# --- HTML entity / whitespace handling --------------------------------------


def test_decodes_html_entities_in_department_name(httpx_mock) -> None:
    """`G&amp;A` should surface as `G&A` — important for departments like
    `Sales & Marketing` that come through with literal entities."""
    html = (
        '<li id="bhrDepartmentID_1" class="BambooHR-ATS-Department-Item">'
        '<div id="department_1" class="BambooHR-ATS-Department-Header">G&amp;A</div>'
        '<ul class="BambooHR-ATS-Jobs-List">'
        '<li id="bhrPositionID_10" class="BambooHR-ATS-Jobs-Item">'
        '<a href="//acme.bamboohr.com/careers/10">Junior Acct</a>'
        '<span class="BambooHR-ATS-Location">Calgary, AB</span></li>'
        '</ul></li>'
    )
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert jobs[0].department == "G&A"


def test_decodes_html_entities_in_title(httpx_mock) -> None:
    html = _widget_html([
        {"id": 1, "name": "Eng", "jobs": [
            {"id": 10, "title": "R&amp;D Engineer (C/C++)", "location": "X"}
        ]},
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert jobs[0].title == "R&D Engineer (C/C++)"


def test_collapses_whitespace_in_title(httpx_mock) -> None:
    """Real BambooHR widget HTML has tabs and newlines around anchor text
    (``<a>\\n\\t Title \\n</a>``). The output title must be a single
    clean line."""
    html = (
        '<li id="bhrDepartmentID_1" class="BambooHR-ATS-Department-Item">'
        '<div id="department_1" class="BambooHR-ATS-Department-Header">Eng</div>'
        '<ul class="BambooHR-ATS-Jobs-List">'
        '<li id="bhrPositionID_10" class="BambooHR-ATS-Jobs-Item">'
        '<a href="//acme.bamboohr.com/careers/10">\n\t  Senior  Engineer\n</a></li>'
        '</ul></li>'
    )
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert jobs[0].title == "Senior Engineer"


# --- URL forms --------------------------------------------------------------


def test_normalizes_protocol_relative_url(httpx_mock) -> None:
    """BambooHR's anchor hrefs are protocol-relative (`//tenant...`).
    They must end up as full https URLs."""
    html = _widget_html([
        {"id": 1, "name": "Eng", "jobs": [{
            "id": 100, "title": "X", "location": "Y",
            "href": "//acme.bamboohr.com/careers/100",
        }]},
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert str(jobs[0].url) == "https://acme.bamboohr.com/careers/100"


def test_handles_relative_url(httpx_mock) -> None:
    html = _widget_html([
        {"id": 1, "name": "Eng", "jobs": [{
            "id": 100, "title": "X", "location": "Y",
            "href": "/careers/100",
        }]},
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert str(jobs[0].url) == "https://acme.bamboohr.com/careers/100"


def test_keeps_absolute_url(httpx_mock) -> None:
    html = _widget_html([
        {"id": 1, "name": "Eng", "jobs": [{
            "id": 100, "title": "X", "location": "Y",
            "href": "https://elsewhere.example.com/job/100",
        }]},
    ])
    httpx_mock.add_response(url=WIDGET_URL, text=html)
    jobs = BambooHRScraper("acme").fetch()
    assert str(jobs[0].url) == "https://elsewhere.example.com/job/100"


# --- Error handling & retries ----------------------------------------------


def test_raises_company_not_found_on_404(httpx_mock) -> None:
    httpx_mock.add_response(url=WIDGET_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        BambooHRScraper("acme").fetch()


def test_404_does_not_retry(monkeypatch, httpx_mock) -> None:
    """404 means "this tenant doesn't exist" — retrying wastes time. We only
    register ONE 404 response; if a retry fires, the second request 500s
    against the empty mock queue."""
    import jobhive.scrapers.bamboohr as bh
    monkeypatch.setattr(bh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=WIDGET_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        BambooHRScraper("acme").fetch()


def test_retries_on_5xx_then_succeeds(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.bamboohr as bh
    monkeypatch.setattr(bh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=WIDGET_URL, status_code=503)
    httpx_mock.add_response(url=WIDGET_URL, text=_widget_html([
        {"id": 1, "name": "Eng", "jobs": [{"id": 1, "title": "X", "location": "Y"}]},
    ]))
    jobs = BambooHRScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_retries_on_429_then_succeeds(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.bamboohr as bh
    monkeypatch.setattr(bh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=WIDGET_URL, status_code=429)
    httpx_mock.add_response(url=WIDGET_URL, text=_widget_html([
        {"id": 1, "name": "Eng", "jobs": [{"id": 1, "title": "X", "location": "Y"}]},
    ]))
    jobs = BambooHRScraper("acme").fetch()
    assert len(jobs) == 1


def test_429_with_retry_after_is_honored(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.bamboohr as bh
    monkeypatch.setattr(bh, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=WIDGET_URL, status_code=429, headers={"Retry-After": "9"}
    )
    httpx_mock.add_response(url=WIDGET_URL, text=_widget_html([
        {"id": 1, "name": "Eng", "jobs": [{"id": 1, "title": "X", "location": "Y"}]},
    ]))
    BambooHRScraper("acme").fetch()
    assert 9.0 in sleeps


def test_5xx_exhausts_and_raises(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.bamboohr as bh
    monkeypatch.setattr(bh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=WIDGET_URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        BambooHRScraper("acme").fetch()


def test_network_error_raises_after_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.bamboohr as bh
    monkeypatch.setattr(bh, "MAX_RETRIES", 2)
    httpx_mock.add_exception(
        httpx.ConnectError("DNS lookup failed"),
        url=WIDGET_URL,
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="DNS lookup failed"):
        BambooHRScraper("acme").fetch()


# --- Detail-API enrichment --------------------------------------------------


def _detail_payload(**fields: Any) -> dict[str, Any]:
    """Build a minimal `/careers/{id}/detail` JSON response."""
    return {"meta": {}, "result": {"jobOpening": fields}}


def test_descriptions_enriched_via_detail_api(httpx_mock) -> None:
    httpx_mock.add_response(url=WIDGET_URL, text=_widget_html([
        {"id": 1, "name": "Eng", "jobs": [{"id": 100, "title": "X", "location": "Y"}]},
    ]))
    httpx_mock.add_response(
        url="https://acme.bamboohr.com/careers/100/detail",
        json=_detail_payload(
            description=(
                "<p>Join our team of <strong>builders</strong>.</p>"
                "<p>Salary negotiable.</p>"
            ),
            employmentStatusLabel="Full-Time",
            datePosted="2026-04-01",
            compensation="$80,000 to $120,000",
        ),
    )
    job = BambooHRScraper("acme").fetch()[0]
    assert job.description is not None
    assert "Join our team of builders" in job.description
    assert "<p>" not in job.description  # tags stripped
    assert job.employment_type == "FULL_TIME"
    assert job.salary_summary == "$80,000 to $120,000"
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_description_failure_keeps_listing_row(httpx_mock) -> None:
    """A 500 on one job's detail call must not lose the listing-derived row."""
    httpx_mock.add_response(url=WIDGET_URL, text=_widget_html([
        {"id": 1, "name": "Eng", "jobs": [
            {"id": 100, "title": "Has Desc", "location": "X"},
            {"id": 200, "title": "No Desc",  "location": "Y"},
        ]},
    ]))
    httpx_mock.add_response(
        url="https://acme.bamboohr.com/careers/100/detail",
        json=_detail_payload(description="<p>Working</p>"),
    )
    httpx_mock.add_response(
        url="https://acme.bamboohr.com/careers/200/detail", status_code=500
    )
    jobs = sorted(BambooHRScraper("acme").fetch(), key=lambda j: j.ats_id)
    assert jobs[0].description == "Working"
    assert jobs[1].description is None  # 500 → None, not raise


def test_description_is_truncated(httpx_mock) -> None:
    """Long descriptions are capped — both at the Pydantic field-level cap
    (25k chars) and our scraper-side cap (12kB) — so memory stays bounded."""
    huge = "Lorem ipsum dolor sit amet. " * 800  # ~22kB
    httpx_mock.add_response(url=WIDGET_URL, text=_widget_html([
        {"id": 1, "name": "Eng", "jobs": [{"id": 100, "title": "X", "location": "Y"}]},
    ]))
    httpx_mock.add_response(
        url="https://acme.bamboohr.com/careers/100/detail",
        json=_detail_payload(description=f"<p>{huge}</p>"),
    )
    job = BambooHRScraper("acme").fetch()[0]
    assert job.description is not None
    assert len(job.description) <= 25_000
