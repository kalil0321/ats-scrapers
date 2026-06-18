"""Tests for the Bdjobs (Bangladesh) scraper.

Pin parsing of the ``GetJobSearch`` payload (jobid, title, company,
JobLang→language, BD country-iso, jobContext+jobDescription concat
+ HTML strip, ISO date) and the seed-segmented dedup behaviour
(unfiltered base feed plus a curated keyword / category seed list,
deduped by ``Jobid``).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import BdjobsScraper, ScraperRegistry

_API = "https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch"
_API_RE = re.compile(r"^https://api\.bdjobs\.com/Jobs/api/JobSearch/GetJobSearch")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.bdjobs as bd
    monkeypatch.setattr(bd, "MAX_RETRIES", 1)
    monkeypatch.setattr(bd, "RETRY_BASE_DELAY", 0.0)


def _job(
    *,
    job_id: str = "1487391",
    title: str = "Client Relationship Executive",
    company: str = "Pattern Properties Limited",
    location: str = "Rajshahi Sadar",
    lang: str = "1",
    experience: str = "At least 1 years",
    publish_date: str = "2026-05-11T23:34:00Z",
    deadline_db: str = "2026-06-10T18:00:00Z",
    job_context: str | None = None,
    job_description: str = "<ul><li><p>Minimum HSC / Bachelor running preferred.</p></li></ul>",
    edu_rec: str | None = None,
) -> dict[str, Any]:
    return {
        "Jobid": job_id,
        "AdType": "2",
        "jobTitle": title,
        "companyName": company,
        "JobTitleBng": title,
        "deadline": "Jun 10, 2026",
        "deadlineDB": deadline_db,
        "publishDate": publish_date,
        "eduRec": edu_rec or "",
        "experience": experience,
        "standout": 0,
        "logo": "",
        "lantype": 0,
        "location": location,
        "JobLang": lang,
        "jobContext": job_context,
        "isEarlyAccess": False,
        "OnlineJob": True,
        "logoUrl": "",
        "jobDescription": job_description,
    }


def _payload(
    premium: list[dict[str, Any]] | None = None,
    data: list[dict[str, Any]] | None = None,
    *,
    total: int = 1,
    total_pages: int = 1,
) -> dict[str, Any]:
    return {
        "message": "Success",
        "statuscode": "1",
        "data": data or [],
        "premiumData": premium or [],
        "common": {
            "total_records_found": total,
            "showd": "1",
            "totalpages": total_pages,
            "total_vacancies": total,
        },
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_bdjobs() -> None:
    assert ScraperRegistry.get(ATSType.BDJOBS) is BdjobsScraper


def test_ats_type_value() -> None:
    assert ATSType.BDJOBS.value == "bdjobs"


# --- happy path -------------------------------------------------------------


def test_parses_full_premium_job(httpx_mock) -> None:
    """Premium-data row → Job. Verifies country_iso=BD, language=en
    (JobLang='1'), description concat + HTML strip, ISO date parse,
    and ``raw`` overflow capture of experience / education /
    deadline."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload(premium=[_job(
            job_context="<p>Build client relationships.</p>",
            job_description="<ul><li>HSC required</li></ul>",
            edu_rec="Bachelor preferred",
        )]),
        is_reusable=True,
    )

    jobs = BdjobsScraper(
        "any", keyword_seeds=(), category_ids=(),
    ).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.BDJOBS
    assert j.ats_id == "1487391"
    assert j.title == "Client Relationship Executive"
    assert j.company == "Pattern Properties Limited"
    assert j.country_iso == "BD"
    assert j.language == "en"
    assert j.location == "Rajshahi Sadar"
    assert str(j.url) == "https://jobs.bdjobs.com/jobdetails.asp?id=1487391&ln=1"
    # Description must concatenate context+description+education and
    # strip HTML.
    assert j.description is not None
    assert "Build client relationships" in j.description
    assert "HSC required" in j.description
    assert "Bachelor preferred" in j.description
    assert "<" not in j.description and ">" not in j.description
    # Posted_at parsed from ISO Z string.
    assert j.posted_at is not None
    assert j.posted_at.year == 2026 and j.posted_at.month == 5
    assert j.fetched_at.tzinfo is not None
    # ``raw`` captures the rich fields the canonical schema can't hold.
    assert j.raw is not None
    assert j.raw["experience"] == "At least 1 years"
    assert j.raw["deadline"] == "2026-06-10T18:00:00Z"
    assert j.raw["education"] == "Bachelor preferred"
    assert j.raw["title_bn"] == j.title  # mirrored in JobTitleBng for english listings


def test_bengali_listing_maps_to_bn_language(httpx_mock) -> None:
    """JobLang='2' → language='bn' and ln=2 in the detail URL."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload(premium=[_job(job_id="555", lang="2")]),
        is_reusable=True,
    )

    jobs = BdjobsScraper("any", keyword_seeds=(), category_ids=()).fetch()
    assert len(jobs) == 1
    assert jobs[0].language == "bn"
    assert "ln=2" in str(jobs[0].url)


# --- pagination / dedup -----------------------------------------------------


def test_data_and_premium_arrays_both_consumed(httpx_mock) -> None:
    """The endpoint may put rows in EITHER ``data`` OR ``premiumData``;
    parser unions both arrays."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload(
            premium=[_job(job_id="a-1", title="From Premium")],
            data=[_job(job_id="a-2", title="From Data")],
        ),
        is_reusable=True,
    )

    jobs = BdjobsScraper("any", keyword_seeds=(), category_ids=()).fetch()
    ids = {j.ats_id for j in jobs}
    assert ids == {"a-1", "a-2"}


def test_seed_segmentation_dedupes_across_queries(httpx_mock) -> None:
    """Base + keyword + category queries each return their own slice;
    overlapping Jobids must be deduped to ``len(unique)`` jobs."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload(premium=[
            _job(job_id="shared-1", title="Shared Job"),
            _job(job_id="base-only", title="Base Only"),
            _job(job_id="kw-only", title="Keyword Only"),
        ]),
        is_reusable=True,
    )

    jobs = BdjobsScraper(
        "any",
        keyword_seeds=("manager", "engineer"),
        category_ids=(2, 3),
    ).fetch()
    # All seeds hit the same mock → dedup down to 3 unique rows.
    assert len(jobs) == 3
    assert {j.ats_id for j in jobs} == {"shared-1", "base-only", "kw-only"}


def test_fetch_query_walks_all_reported_pages(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}$"),
        json=_payload(premium=[_job(job_id="page-1")], total=2, total_pages=2),
    )
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}\?pg=2$"),
        json=_payload(premium=[_job(job_id="page-2")], total=2, total_pages=2),
    )

    jobs = BdjobsScraper("any", keyword_seeds=(), category_ids=()).fetch()

    assert [j.ats_id for j in jobs] == ["page-1", "page-2"]


def test_keyword_and_category_params_are_sent(httpx_mock) -> None:
    """Verify the scraper passes ``Keyword`` and ``Category`` query
    params (case matters — bdjobs is case-sensitive on these)."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload(premium=[_job()]),
        is_reusable=True,
    )

    BdjobsScraper(
        "any",
        keyword_seeds=("engineer",),
        category_ids=(7,),
    ).fetch()

    sent_params: list[dict[str, list[str]]] = []
    for r in httpx_mock.get_requests():
        sent_params.append(parse_qs(urlparse(str(r.url)).query))

    # Three calls expected: base (empty params) + 1 keyword + 1 category.
    assert len(sent_params) == 3
    assert {"Keyword": ["engineer"]} in sent_params
    assert {"Category": ["7"]} in sent_params
    # The base call sends no params.
    assert {} in sent_params


# --- error handling ---------------------------------------------------------


def test_missing_jobid_or_title_is_skipped(httpx_mock) -> None:
    """Rows missing required fields are dropped silently — no
    fallback UUID, no half-formed Job objects."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload(premium=[
            _job(job_id="ok-1", title="Valid"),
            {"Jobid": "", "jobTitle": "No Id"},
            {"Jobid": "x-2", "jobTitle": ""},
        ]),
        is_reusable=True,
    )

    jobs = BdjobsScraper("any", keyword_seeds=(), category_ids=()).fetch()
    assert {j.ats_id for j in jobs} == {"ok-1"}


def test_seed_query_failure_is_demoted_to_warning(
    httpx_mock, caplog: pytest.LogCaptureFixture,
) -> None:
    """A 500 on one keyword seed must not poison the whole sweep —
    the base feed and the other seeds still contribute their rows."""
    # Base call succeeds.
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}$"),
        json=_payload(premium=[_job(job_id="base-1", title="Base Job")]),
    )
    # The failing keyword seed.
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}\?Keyword=engineer$"),
        status_code=500,
    )
    # The succeeding category seed.
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}\?Category=5$"),
        json=_payload(premium=[_job(job_id="cat-1", title="Cat Job")]),
    )

    with caplog.at_level("WARNING"):
        jobs = BdjobsScraper(
            "any", keyword_seeds=("engineer",), category_ids=(5,),
        ).fetch()
    assert {j.ats_id for j in jobs} == {"base-1", "cat-1"}
    assert any("bdjobs" in r.message.lower() for r in caplog.records)


def test_base_failure_after_retries_raises(httpx_mock) -> None:
    """Sustained server error on the *base* call (no params) raises
    rather than silently returning ``[]`` — the failure mode must be
    loud so monitoring sees it."""
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}$"),
        status_code=500,
    )
    with pytest.raises(ScraperError, match="bdjobs"):
        BdjobsScraper("any", keyword_seeds=(), category_ids=()).fetch()


def test_malformed_json_payload_raises_scraper_error(httpx_mock) -> None:
    """A 200 response must still have the expected object shape."""
    httpx_mock.add_response(
        url=re.compile(rf"^{re.escape(_API)}$"),
        json=[{"not": "a payload object"}],
    )
    with pytest.raises(ScraperError, match="malformed JSON"):
        BdjobsScraper("any", keyword_seeds=(), category_ids=()).fetch()
