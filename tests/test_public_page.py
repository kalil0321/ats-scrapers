"""Boards whose public page is closed while the provider still lists postings."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError
from ats_scrapers.scrapers import GemScraper, LeverScraper, RecruiteeScraper

LEVER_API = "https://api.lever.co/v0/postings/acme?mode=json"
LEVER_BOARD = "https://jobs.lever.co/acme"
GEM_API = "https://jobs.gem.com/api/public/graphql/batch"
GEM_BOARD = "https://jobs.gem.com/acme"
RECRUITEE_API = "https://acme.recruitee.com/api/offers"
RECRUITEE_SITE = "https://acme.recruitee.com/"


def _lever_job() -> dict:
    return {
        "id": "x1",
        "text": "SWE",
        "hostedUrl": "https://jobs.lever.co/acme/x1",
        "categories": {"location": "Remote"},
        "createdAt": 1714521600000,
    }


def _gem_list() -> list[dict]:
    posting = {"id": "internal-id", "extId": "ext-1", "title": "ML Engineer"}
    return [{"data": {"oatsExternalJobPostings": {"jobPostings": [posting]}}}]


def _recruitee_offers() -> dict:
    return {
        "offers": [
            {
                "id": 1,
                "title": "Senior Engineer",
                "careers_url": "https://acme.recruitee.com/o/1",
                "created_at": "2026-04-15T08:00:00Z",
            }
        ]
    }


# --- Lever


def test_lever_board_with_a_closed_page_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=LEVER_API, json=[_lever_job()])
    httpx_mock.add_response(method="HEAD", url=LEVER_BOARD, status_code=404)

    with pytest.raises(CompanyNotFoundError, match="board page is closed"):
        LeverScraper("acme").fetch()


def test_lever_checks_the_eu_board_page_for_an_eu_board(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.eu.lever.co/v0/postings/acme?mode=json",
        json=[_lever_job()],
    )
    httpx_mock.add_response(
        method="HEAD", url="https://jobs.eu.lever.co/acme", status_code=404
    )

    with pytest.raises(CompanyNotFoundError):
        LeverScraper("acme", region="eu").fetch()


def test_lever_keeps_postings_when_the_page_check_is_inconclusive(httpx_mock) -> None:
    httpx_mock.add_response(url=LEVER_API, json=[_lever_job()])
    httpx_mock.add_response(method="HEAD", url=LEVER_BOARD, status_code=400)

    assert len(LeverScraper("acme").fetch()) == 1


def test_lever_empty_board_is_not_checked(httpx_mock) -> None:
    httpx_mock.add_response(url=LEVER_API, json=[])

    assert LeverScraper("acme").fetch() == []


# --- Gem


def test_gem_board_with_a_closed_page_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=GEM_API, json=_gem_list())
    httpx_mock.add_response(method="HEAD", url=GEM_BOARD, status_code=404)

    with pytest.raises(CompanyNotFoundError, match="board page is closed"):
        GemScraper("acme", include_descriptions=False).fetch()


def test_gem_keeps_postings_when_the_page_answers(httpx_mock) -> None:
    httpx_mock.add_response(url=GEM_API, json=_gem_list())
    httpx_mock.add_response(method="HEAD", url=GEM_BOARD)

    assert len(GemScraper("acme", include_descriptions=False).fetch()) == 1


# --- Recruitee


def test_recruitee_site_sent_to_the_not_hosted_page_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=RECRUITEE_API, json=_recruitee_offers())
    httpx_mock.add_response(
        method="HEAD",
        url=RECRUITEE_SITE,
        status_code=301,
        headers={"Location": "https://recruitee.com/careers_not_hosted"},
    )

    with pytest.raises(CompanyNotFoundError, match="careers site is closed"):
        RecruiteeScraper("acme").fetch()


def test_recruitee_site_redirecting_to_its_own_domain_is_kept(httpx_mock) -> None:
    httpx_mock.add_response(url=RECRUITEE_API, json=_recruitee_offers())
    httpx_mock.add_response(
        method="HEAD",
        url=RECRUITEE_SITE,
        status_code=302,
        headers={"Location": "https://careers.acme.example/"},
    )

    assert len(RecruiteeScraper("acme").fetch()) == 1


def test_recruitee_without_offers_is_not_checked(httpx_mock) -> None:
    httpx_mock.add_response(url=RECRUITEE_API, json={"offers": []})

    assert RecruiteeScraper("acme").fetch() == []
