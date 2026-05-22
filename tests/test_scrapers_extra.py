"""Tests for the 11 ported scrapers.

Each scraper gets a happy-path test plus a 404 / not-found test where the
ATS protocol supports it. We mock httpx so no network traffic.
"""

from __future__ import annotations

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.scrapers import (
    GemScraper,
    JoinComScraper,
    OracleScraper,
    PersonioScraper,
    RipplingScraper,
    SmartRecruitersScraper,
    WorkableScraper,
    WorkdayScraper,
)

# Several scrapers now fan out per-job detail fetches after the listing
# pass (Gem batched GraphQL, join.com JSON-LD, etc.). Tests below mock
# only the listing call and rely on this module-level relax-mark to
# tolerate the unmatched detail requests.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)

# --- SmartRecruiters ---------------------------------------------------------

def test_smartrecruiters_happy_path(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=0",
        json={
            "content": [
                {
                    "id": "abc-1",
                    "name": "Senior Engineer",
                    "location": {"city": "Berlin", "country": "DE"},
                    "releasedDate": "2026-04-01T10:00:00Z",
                }
            ]
        },
    )
    jobs = SmartRecruitersScraper("acme").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Engineer"
    assert jobs[0].location == "Berlin, DE"
    assert jobs[0].ats_id == "abc-1"


def test_smartrecruiters_paginates(httpx_mock) -> None:
    page_one = [{"id": str(i), "name": "T", "location": {"city": "X"}} for i in range(100)]
    httpx_mock.add_response(
        url="https://api.smartrecruiters.com/v1/companies/big/postings?limit=100&offset=0",
        json={"content": page_one},
    )
    httpx_mock.add_response(
        url="https://api.smartrecruiters.com/v1/companies/big/postings?limit=100&offset=100",
        json={"content": [{"id": "100", "name": "T", "location": {"city": "X"}}]},
    )
    jobs = SmartRecruitersScraper("big").fetch()
    assert len(jobs) == 101


def test_smartrecruiters_404(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.smartrecruiters.com/v1/companies/missing/postings?limit=100&offset=0",
        status_code=404,
    )
    with pytest.raises(CompanyNotFoundError):
        SmartRecruitersScraper("missing").fetch()


# --- Workable ----------------------------------------------------------------

def test_workable_happy_path(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://apply.workable.com/api/v1/widget/accounts/acme",
        json={
            "jobs": [
                {
                    "shortcode": "ABC123",
                    "title": "Backend Dev",
                    "url": "https://apply.workable.com/acme/j/ABC123",
                    "location": {"city": "Paris", "country": "France"},
                    "published_on": "2026-03-15T10:00:00Z",
                }
            ]
        },
    )
    jobs = WorkableScraper("acme").fetch()
    assert jobs[0].title == "Backend Dev"
    assert jobs[0].location == "Paris, France"
    assert jobs[0].ats_id == "ABC123"


def test_workable_404(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://apply.workable.com/api/v1/widget/accounts/missing",
        status_code=404,
    )
    with pytest.raises(CompanyNotFoundError):
        WorkableScraper("missing").fetch()


# --- Rippling ----------------------------------------------------------------

def test_rippling_happy_path_with_items_envelope(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.rippling.com/platform/api/ats/v1/board/acme/jobs",
        json={
            "items": [
                {
                    "id": "xyz",
                    "name": "Engineer",
                    "url": "https://ats.rippling.com/acme/jobs/xyz",
                    "workLocation": {"displayName": "Remote"},
                    "createdAt": "2026-04-01T00:00:00Z",
                }
            ]
        },
    )
    jobs = RipplingScraper("acme").fetch()
    assert jobs[0].title == "Engineer"
    assert jobs[0].location == "Remote"


def test_rippling_handles_bare_list(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.rippling.com/platform/api/ats/v1/board/acme/jobs",
        json=[
            {
                "id": "xyz",
                "title": "Engineer",
                "url": "https://ats.rippling.com/acme/jobs/xyz",
                "location": "Remote",
            }
        ],
    )
    jobs = RipplingScraper("acme").fetch()
    assert len(jobs) == 1


def test_rippling_404(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.rippling.com/platform/api/ats/v1/board/missing/jobs",
        status_code=404,
    )
    with pytest.raises(CompanyNotFoundError):
        RipplingScraper("missing").fetch()


# --- Personio ----------------------------------------------------------------

def test_personio_search_endpoint_first(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://acme.jobs.personio.com/search.json",
        json=[
            {"id": 1, "name": "Designer", "office": "Munich"},
            {"id": 2, "name": "PM", "office": "Berlin"},
        ],
    )
    jobs = PersonioScraper("acme").fetch()
    assert {j.title for j in jobs} == {"Designer", "PM"}


def test_personio_falls_back_to_careers_api(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://acme.jobs.personio.com/search.json",
        status_code=404,
    )
    httpx_mock.add_response(
        url="https://acme.jobs.personio.com/api/careers/jobs/list/",
        json={"data": [{"id": "abc", "title": "Backend", "location": {"name": "Berlin"}}]},
    )
    jobs = PersonioScraper("acme").fetch()
    assert jobs[0].title == "Backend"
    assert jobs[0].location == "Berlin"


def test_personio_accepts_full_url(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://custom.example.com/search.json",
        json=[{"id": 1, "name": "Engineer"}],
    )
    jobs = PersonioScraper("https://custom.example.com").fetch()
    assert len(jobs) == 1


# --- Mercor: covered in test_mercor.py --------------------------------------

# --- Gem ---------------------------------------------------------------------

def test_gem_parses_jobpostings(httpx_mock) -> None:
    # The scraper hits the GraphQL endpoint twice: once for the list
    # (``JobBoardList``) and once for the batched detail enrichment
    # (``ExternalJobPostingQuery``). The list mock below is the only one
    # that matters for this test; the detail call is tolerated by the
    # module-level ``assert_all_requests_were_expected=False`` mark.
    httpx_mock.add_response(
        url="https://jobs.gem.com/api/public/graphql/batch",
        json=[
            {
                "data": {
                    "oatsExternalJobPostings": {
                        "jobPostings": [
                            {
                                "id": "internal-id",
                                "extId": "ext-1",
                                "title": "ML Engineer",
                                "locations": [
                                    {"city": "San Francisco", "isoCountry": "USA"}
                                ],
                            }
                        ]
                    }
                }
            }
        ],
    )
    jobs = GemScraper("acme").fetch()
    assert jobs[0].title == "ML Engineer"
    assert jobs[0].location == "San Francisco, USA"
    assert str(jobs[0].url) == "https://jobs.gem.com/acme/ext-1"


def test_gem_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.gem.com/api/public/graphql/batch",
        json=[{"errors": [{"message": "Board not found"}], "data": None}],
    )
    with pytest.raises(CompanyNotFoundError):
        GemScraper("ghost").fetch()


def test_gem_empty_response(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.gem.com/api/public/graphql/batch",
        json=[{"data": {"oatsExternalJobPostings": {"jobPostings": []}}}],
    )
    assert GemScraper("empty").fetch() == []


# --- Join.com ----------------------------------------------------------------

def test_join_com_resolves_id_and_lists_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://join.com/companies/acme",
        text='<html><script>{"company":{"id":"42","slug":"acme"}}</script></html>',
    )
    httpx_mock.add_response(
        url=(
            "https://join.com/api/public/companies/42/jobs"
            "?locale=en-us&page=1&pageSize=100&withAggregations=true&sort=%2Btitle"
        ),
        json={
            "items": [{"id": 100, "title": "Designer", "location": "Berlin"}],
            "pagination": {"totalPages": 1},
        },
    )
    jobs = JoinComScraper("acme").fetch()
    assert jobs[0].title == "Designer"
    assert jobs[0].ats_id == "100"


def test_join_com_404(httpx_mock) -> None:
    httpx_mock.add_response(url="https://join.com/companies/missing", status_code=404)
    with pytest.raises(CompanyNotFoundError):
        JoinComScraper("missing").fetch()


# --- Workday -----------------------------------------------------------------

def test_workday_parses_url_and_paginates(httpx_mock) -> None:
    api = "https://accenture.wd103.myworkdayjobs.com/wday/cxs/accenture/accenturecareers/jobs"
    page_one_postings = [
        {
            "title": f"Job {i}",
            "externalPath": f"/job/{i}",
            "locationsText": "Worldwide",
            "bulletFields": [f"R{i}"],
            "postedOn": "Posted Yesterday",
        }
        for i in range(20)
    ]
    # First response carries `total` so the async planner knows how many
    # extra pages to fan out.
    httpx_mock.add_response(
        url=api, json={"jobPostings": page_one_postings, "total": 21}
    )
    httpx_mock.add_response(
        url=api,
        json={
            "jobPostings": [
                {
                    "title": "Last One",
                    "externalPath": "/job/99",
                    "locationsText": "NYC",
                    "bulletFields": ["R99"],
                }
            ],
            "total": 21,
        },
    )
    jobs = WorkdayScraper(
        "https://accenture.wd103.myworkdayjobs.com/accenturecareers"
    ).fetch()
    assert len(jobs) == 21
    titles = {j.title for j in jobs}
    assert "Last One" in titles


def test_workday_dedupes_overlapping_pages(httpx_mock) -> None:
    """Concurrent paginated fetches can return the same job twice when the
    underlying listing shifts. Dedup must collapse them to a single Job."""
    api = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    page_one = [
        {"title": f"Job {i}", "externalPath": f"/job/{i}", "bulletFields": [f"R{i}"]}
        for i in range(20)
    ]
    # Second page repeats the last 5 of page one (R15..R19) and adds 5 new
    page_two = [
        {"title": f"Job {i}", "externalPath": f"/job/{i}", "bulletFields": [f"R{i}"]}
        for i in range(15, 25)
    ]
    httpx_mock.add_response(url=api, json={"jobPostings": page_one, "total": 25})
    httpx_mock.add_response(url=api, json={"jobPostings": page_two, "total": 25})

    jobs = WorkdayScraper("https://acme.wd1.myworkdayjobs.com/External").fetch()
    ats_ids = [j.ats_id for j in jobs]
    assert len(jobs) == 25
    assert len(set(ats_ids)) == 25  # no duplicates
    assert ats_ids == sorted(ats_ids, key=lambda s: int(s[1:]))  # ordered correctly


def test_workday_short_response_returns_only_first_page(httpx_mock) -> None:
    api = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    httpx_mock.add_response(
        url=api,
        json={
            "jobPostings": [
                {"title": "Only", "externalPath": "/job/1", "bulletFields": ["R1"]}
            ],
            "total": 1,
        },
    )
    jobs = WorkdayScraper("https://acme.wd1.myworkdayjobs.com/External").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Only"


def test_workday_uses_configured_company_name(httpx_mock) -> None:
    api = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    httpx_mock.add_response(
        url=api,
        json={
            "jobPostings": [
                {"title": "Only", "externalPath": "/job/1", "bulletFields": ["R1"]}
            ],
            "total": 1,
        },
    )

    jobs = WorkdayScraper(
        "https://acme.wd1.myworkdayjobs.com/External",
        company_name="Acme Health",
    ).fetch()

    assert jobs[0].company == "Acme Health"


def test_workday_invalid_url_raises() -> None:
    with pytest.raises(ScraperError, match="Workday URL"):
        WorkdayScraper("https://example.com").fetch()


def test_workday_resolves_n_locations_rollup(httpx_mock) -> None:
    """When ``locationsText`` is the 'N Locations' rollup, the search API
    doesn't include the actual list — we have to fetch the per-job detail
    endpoint to get them. This was the bug behind 31k Workday rows
    collapsing to placeholder strings like '2 Locations'."""
    api = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    httpx_mock.add_response(
        url=api,
        json={
            "jobPostings": [
                {
                    "title": "Engineer",
                    "externalPath": "/job/USA-NY-NYC/Engineer_R-1",
                    "locationsText": "2 Locations",
                    "bulletFields": ["R-1"],
                },
                # A regular single-location job — must be left alone.
                {
                    "title": "Manager",
                    "externalPath": "/job/USA-CA-SF/Manager_R-2",
                    "locationsText": "San Francisco, CA",
                    "bulletFields": ["R-2"],
                },
            ],
            "total": 2,
        },
    )
    # Detail endpoint for the rollup job — primary + additionalLocations.
    detail_url = (
        "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External"
        "/job/USA-NY-NYC/Engineer_R-1"
    )
    httpx_mock.add_response(
        url=detail_url,
        json={
            "jobPostingInfo": {
                "jobDescription": "<p>Build internal platforms.</p>",
                "location": "USA - NY - New York",
                "additionalLocations": ["USA - CA - San Francisco"],
            }
        },
    )

    jobs = WorkdayScraper("https://acme.wd1.myworkdayjobs.com/External").fetch()
    by_title = {j.title: j.location for j in jobs}
    assert by_title["Engineer"] == "USA - NY - New York | USA - CA - San Francisco"
    by_desc = {j.title: j.description for j in jobs}
    assert by_desc["Engineer"] == "Build internal platforms."
    # Single-location job location stays untouched even though the detail
    # enrichment now also attempts to hydrate descriptions for every row.
    assert by_title["Manager"] == "San Francisco, CA"
    assert by_desc["Manager"] is None


def test_workday_enriches_description_from_detail_endpoint(httpx_mock) -> None:
    api = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    httpx_mock.add_response(
        url=api,
        json={
            "jobPostings": [
                {
                    "title": "Engineer",
                    "externalPath": "/job/USA/Engineer_R-1",
                    "locationsText": "New York, NY",
                    "bulletFields": ["R-1"],
                },
            ],
            "total": 1,
        },
    )
    httpx_mock.add_response(
        url="https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/USA/Engineer_R-1",
        json={
            "jobPostingInfo": {
                "jobDescription": "<div><p>Build <strong>search</strong>.</p></div>",
            }
        },
    )

    jobs = WorkdayScraper("https://acme.wd1.myworkdayjobs.com/External").fetch()

    assert len(jobs) == 1
    assert jobs[0].description == "Build search ."


def test_workday_rollup_resolution_failure_is_silent(httpx_mock) -> None:
    """If the detail fetch 404s or returns malformed JSON, listing fields
    are kept rather than crashing the whole scrape."""
    api = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    httpx_mock.add_response(
        url=api,
        json={
            "jobPostings": [
                {
                    "title": "Engineer",
                    "externalPath": "/job/USA/Engineer_R-1",
                    "locationsText": "3 Locations",
                    "bulletFields": ["R-1"],
                },
            ],
            "total": 1,
        },
    )
    httpx_mock.add_response(
        url="https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/USA/Engineer_R-1",
        status_code=404,
    )
    jobs = WorkdayScraper("https://acme.wd1.myworkdayjobs.com/External").fetch()
    assert len(jobs) == 1
    assert jobs[0].location == "3 Locations"  # original rollup kept
    assert jobs[0].description is None


# --- Avature: covered in test_avature.py ------------------------------------

# --- Phenom: covered in test_phenom.py --------------------------------------

# --- Oracle ------------------------------------------------------------------

def test_oracle_with_default_site(httpx_mock) -> None:
    """Oracle's response wraps jobs in `items[0].requisitionList`. The
    pagination params live INSIDE the `finder` string (not at the top level),
    and `expand=requisitionList` is required to get any actual postings."""
    base = "https://eeho.fa.us2.oraclecloud.com"
    api = f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    httpx_mock.add_response(
        url=(
            f"{api}?onlyData=true"
            f"&finder=findReqs%3BsiteNumber%3DCX_1%2Climit%3D200%2Coffset%3D0"
            f"&expand=requisitionList"
        ),
        json={
            "items": [{
                "TotalJobsCount": 1,
                "requisitionList": [{
                    "Id": "001",
                    "Title": "DBA",
                    "PrimaryLocation": "Redwood Shores",
                    "PostedDate": "2026-03-01T00:00:00Z",
                }],
            }]
        },
    )
    jobs = OracleScraper(base).fetch()
    assert jobs[0].title == "DBA"
    assert jobs[0].location == "Redwood Shores"


def test_oracle_extracts_site_number_from_query(httpx_mock) -> None:
    base = "https://eeho.fa.us2.oraclecloud.com"
    api = f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    httpx_mock.add_response(
        url=(
            f"{api}?onlyData=true"
            f"&finder=findReqs%3BsiteNumber%3DCX_45002%2Climit%3D200%2Coffset%3D0"
            f"&expand=requisitionList"
        ),
        json={"items": [{"TotalJobsCount": 0, "requisitionList": []}]},
    )
    OracleScraper(f"{base}?site_number=CX_45002").fetch()


def test_oracle_accepts_candidate_experience_site_url(httpx_mock) -> None:
    base = "https://eeho.fa.us2.oraclecloud.com"
    api = f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    httpx_mock.add_response(
        url=(
            f"{api}?onlyData=true"
            f"&finder=findReqs%3BsiteNumber%3DCX_45002%2Climit%3D200%2Coffset%3D0"
            f"&expand=requisitionList"
        ),
        json={"items": [{"TotalJobsCount": 0, "requisitionList": []}]},
    )
    OracleScraper(
        f"{base}/hcmUI/CandidateExperience/en/sites/CX_45002"
    ).fetch()


def test_oracle_requires_full_url() -> None:
    with pytest.raises(ScraperError, match="full URL"):
        OracleScraper("eeho").fetch()
