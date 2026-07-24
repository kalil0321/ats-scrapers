"""Tests for the UKG Pro Recruiting scraper."""

from __future__ import annotations

import json

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ukg
from ats_scrapers.scrapers.ukg import UKGProScraper, _normalize_board_url

BOARD = (
    "https://recruiting.ultipro.com/ACM1000/JobBoard/"
    "11111111-2222-3333-4444-555555555555"
)
INTERNAL_BASE = BOARD
LOAD_URL = f"{INTERNAL_BASE}/JobBoardView/LoadSearchResults"


def _board_page(load_url: str = "") -> str:
    target = load_url or (
        "/ACM1000/JobBoard/11111111-2222-3333-4444-555555555555/"
        "JobBoardView/LoadSearchResults"
    )
    return f'<script>var model = {{ loadUrl: "{target}" }};</script>'


def _listing(job_id: str, title: str = "Engineer") -> dict[str, object]:
    return {
        "Id": job_id,
        "Featured": False,
        "Title": title,
        "RequisitionNumber": f"REQ-{job_id[:4]}",
        "FullTime": True,
        "JobCategoryName": "Engineering",
        "Locations": [
            {
                "LocalizedName": "Remote",
                "Address": {
                    "City": "Remote",
                    "State": None,
                    "Country": {"Code": "USA", "Name": "United States"},
                },
                "Coordinates": {"Latitude": 40.0, "Longitude": -75.0},
            }
        ],
        "PostedDate": "2026-07-20T12:00:00Z",
        "JobLocationType": 2,
        "OpportunityType": 0,
    }


def _detail(job_id: str, *, description: str = "<p>Build systems.</p>") -> str:
    payload = {
        "Id": job_id,
        "Description": description,
        "Locations": _listing(job_id)["Locations"],
        "UpdatedDate": "2026-07-21T12:00:00Z",
        "OpportunityIsClosed": False,
        "JobLocationType": 2,
        "CompensationAnnualMinimum": 100000,
        "CompensationAnnualMaximum": 125000,
        "CompensationCurrencyCode": "USD",
    }
    return (
        "<script>var opportunity = new "
        "US.Opportunity.CandidateOpportunityDetail("
        f"{json.dumps(payload)}"
        ");</script>"
    )


def test_fetches_all_pages_and_enriches_details(
    httpx_mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ukg, "PAGE_SIZE", 2)
    ids = [
        "aaaaaaaa-1111-2222-3333-444444444444",
        "bbbbbbbb-1111-2222-3333-444444444444",
        "cccccccc-1111-2222-3333-444444444444",
    ]
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [_listing(ids[0]), _listing(ids[1])], "totalCount": 3},
    )
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [_listing(ids[2])], "totalCount": 3},
    )
    for job_id in ids:
        httpx_mock.add_response(
            url=f"{INTERNAL_BASE}/OpportunityDetail?opportunityId={job_id}",
            text=_detail(job_id),
        )

    jobs = UKGProScraper(BOARD, company_name="Acme").fetch()

    assert [job.ats_id for job in jobs] == ids
    assert all(job.ats_type is ATSType.UKG for job in jobs)
    assert all(job.company == "Acme" for job in jobs)
    assert jobs[0].location == "Remote, United States"
    assert jobs[0].country_iso == "US"
    assert jobs[0].lat == 40.0
    assert jobs[0].lon == -75.0
    assert jobs[0].is_remote is True
    assert jobs[0].employment_type == "FULL_TIME"
    assert jobs[0].department == "Engineering"
    assert jobs[0].description == "Build systems."
    assert jobs[0].salary_min == 100000
    assert jobs[0].salary_max == 125000
    assert jobs[0].salary_currency == "USD"
    assert jobs[0].salary_period == "YEAR"

    post_requests = [
        request
        for request in httpx_mock.get_requests()
        if request.method == "POST"
    ]
    assert [request.read().decode() for request in post_requests] == [
        json.dumps(
            {"opportunitySearch": ukg._search_payload(0)},
            separators=(",", ":"),
        ),
        json.dumps(
            {"opportunitySearch": ukg._search_payload(2)},
            separators=(",", ":"),
        ),
    ]


def test_include_descriptions_false_skips_detail(httpx_mock) -> None:
    job_id = "aaaaaaaa-1111-2222-3333-444444444444"
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [_listing(job_id)], "totalCount": 1},
    )

    jobs = UKGProScraper(BOARD, include_descriptions=False).fetch()

    assert len(jobs) == 1
    assert jobs[0].description is None


def test_internal_board_id_from_page_is_used(httpx_mock) -> None:
    internal_id = "99999999-2222-3333-4444-555555555555"
    internal_base = f"https://recruiting.ultipro.com/ACM1000/JobBoard/{internal_id}"
    job_id = "aaaaaaaa-1111-2222-3333-444444444444"
    httpx_mock.add_response(
        url=BOARD,
        text=_board_page(
            f"/ACM1000/JobBoard/{internal_id}/JobBoardView/LoadSearchResults"
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{internal_base}/JobBoardView/LoadSearchResults",
        json={"opportunities": [_listing(job_id)], "totalCount": 1},
    )

    jobs = UKGProScraper(BOARD, include_descriptions=False).fetch()

    assert str(jobs[0].url).startswith(
        f"{internal_base}/OpportunityDetail"
    )


def test_unsafe_search_endpoint_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=BOARD,
        text=_board_page(
            "https://evil.example/ACM1000/JobBoard/"
            "11111111-2222-3333-4444-555555555555/"
            "JobBoardView/LoadSearchResults"
        ),
    )

    with pytest.raises(ScraperError, match="unsafe search endpoint"):
        UKGProScraper(BOARD).fetch()


def test_reported_total_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [], "totalCount": 1},
    )

    with pytest.raises(ScraperError, match="reported total"):
        UKGProScraper(BOARD, include_descriptions=False).fetch()


def test_explicit_zero_result_catalogue_returns_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [], "totalCount": 0},
    )

    assert UKGProScraper(BOARD).fetch() == []


def test_closed_job_between_listing_and_detail_is_dropped(httpx_mock) -> None:
    ids = [
        "aaaaaaaa-1111-2222-3333-444444444444",
        "bbbbbbbb-1111-2222-3333-444444444444",
    ]
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [_listing(job_id) for job_id in ids], "totalCount": 2},
    )
    httpx_mock.add_response(
        url=f"{INTERNAL_BASE}/OpportunityDetail?opportunityId={ids[0]}",
        text=_detail(ids[0]),
    )
    httpx_mock.add_response(
        url=f"{INTERNAL_BASE}/OpportunityDetail?opportunityId={ids[1]}",
        status_code=404,
    )

    jobs = UKGProScraper(BOARD).fetch()

    assert [job.ats_id for job in jobs] == [ids[0]]


def test_detail_request_failure_keeps_listing_job(httpx_mock) -> None:
    job_id = "aaaaaaaa-1111-2222-3333-444444444444"
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [_listing(job_id)], "totalCount": 1},
    )
    httpx_mock.add_response(
        url=f"{INTERNAL_BASE}/OpportunityDetail?opportunityId={job_id}",
        status_code=500,
        is_reusable=True,
    )

    jobs = UKGProScraper(BOARD).fetch()

    assert [job.ats_id for job in jobs] == [job_id]
    assert jobs[0].description is None


def test_malformed_detail_keeps_listing_job(httpx_mock) -> None:
    ids = [
        "aaaaaaaa-1111-2222-3333-444444444444",
        "bbbbbbbb-1111-2222-3333-444444444444",
    ]
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={
            "opportunities": [_listing(job_id) for job_id in ids],
            "totalCount": 2,
        },
    )
    httpx_mock.add_response(
        url=f"{INTERNAL_BASE}/OpportunityDetail?opportunityId={ids[0]}",
        text=_detail(ids[0]),
    )
    httpx_mock.add_response(
        url=f"{INTERNAL_BASE}/OpportunityDetail?opportunityId={ids[1]}",
        text="<html><body>Malformed detail</body></html>",
    )

    jobs = UKGProScraper(BOARD).fetch()

    assert [job.ats_id for job in jobs] == ids
    assert jobs[0].description == "Build systems."
    assert jobs[1].description is None


def test_empty_description_keeps_listing_job(httpx_mock) -> None:
    job_id = "aaaaaaaa-1111-2222-3333-444444444444"
    httpx_mock.add_response(url=BOARD, text=_board_page())
    httpx_mock.add_response(
        method="POST",
        url=LOAD_URL,
        json={"opportunities": [_listing(job_id)], "totalCount": 1},
    )
    httpx_mock.add_response(
        url=f"{INTERNAL_BASE}/OpportunityDetail?opportunityId={job_id}",
        text=_detail(job_id, description=""),
    )

    jobs = UKGProScraper(BOARD).fetch()

    assert [job.ats_id for job in jobs] == [job_id]
    assert jobs[0].description is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://recruiting.ultipro.com/ACM1000/JobBoard/"
        "11111111-2222-3333-4444-555555555555",
        "https://evil.example/ACM1000/JobBoard/"
        "11111111-2222-3333-4444-555555555555",
        "https://recruiting.ultipro.com/bad_slug/JobBoard/"
        "11111111-2222-3333-4444-555555555555",
        "https://recruiting.ultipro.com/ACM1000/JobBoard/not-a-uuid",
    ],
)
def test_rejects_invalid_board_urls(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_board_url(value)


def test_normalizes_supported_hosts() -> None:
    assert _normalize_board_url(
        "recruiting2.ultipro.com/ACM1000/JobBoard/"
        "11111111-2222-3333-4444-555555555555/"
    ) == (
        "https://recruiting2.ultipro.com/ACM1000/JobBoard/"
        "11111111-2222-3333-4444-555555555555"
    )
