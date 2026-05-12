"""Tests for the Green Japan scraper.

Green-Japan is a Next.js SSR site whose listing payload is reachable in
two equivalent forms: embedded in the ``__NEXT_DATA__`` script on the
``/search_key`` HTML page, and as JSON at ``_next/data/{buildId}/
search.json?page=N``. The scraper grabs both buildId and page-1 from
the HTML in one shot, then walks the JSON endpoint for the remaining
pages. ``defaultSearchJobOfferData.totalJobOfferCount`` decides when to
stop.

Tests cover:
- HTML discovery seeds buildId + page-1 jobs + total
- Subsequent pages use the JSON endpoint
- A 404 on the JSON endpoint triggers re-discovery and resumes
- Mapping of the embedded jobOffer fields → Job slots (skills, salary)
- The total-count cap stops pagination before any tail-empty walk
"""

from __future__ import annotations

import json
import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import GreenJapanScraper, ScraperRegistry
from jobhive.scrapers.greenjapan import (
    _parse_jpy_salary,
    _parse_unix_timestamp,
)

_LISTING_URL = "https://www.green-japan.com/search_key"
_DATA_RE = re.compile(
    r"^https://www\.green-japan\.com/_next/data/[^/]+/search\.json\?page=\d+$"
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.greenjapan as g
    monkeypatch.setattr(g, "MAX_RETRIES", 1)
    monkeypatch.setattr(g, "RETRY_BASE_DELAY", 0.0)


def _job(
    *,
    id: int = 235863,
    name: str = "システム開発エンジニア",
    title: str | None = "賞与実績 5.2ヶ月分",
    company_name: str = "株式会社 D・Ace",
    company_id: int = 9939,
    company_title: str = "「楽する」ために工夫し楽しむ。",
    salary: str = "410万円〜800万円",
    area_name: str = "東京都, 神奈川県, 千葉県",
    skill_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    industry: str = "自由で効率的なシステム開発サービス",
    updated_at: int = 1778459021,
    job_offer_url: str | None = None,
) -> dict:
    skill_names = skill_names if skill_names is not None else [
        "Python", "TypeScript", "Go",
    ]
    tag_names = tag_names if tag_names is not None else ["リモート勤務の相談可"]
    payload: dict = {
        "id": id,
        "name": name,
        "company": {
            "id": company_id,
            "name": company_name,
            "title": company_title,
        },
        "salary": salary,
        "areaName": area_name,
        "skillNames": skill_names,
        "tagNames": tag_names,
        "clientBusiness": {"name": industry},
        "jobOfferUpdatedAtTimestamp": updated_at,
        "jobOfferUrl": (
            job_offer_url
            if job_offer_url is not None
            else f"/company/{company_id}/job/{id}"
        ),
    }
    if title is not None:
        payload["title"] = title
    return payload


def _next_data(
    *, build_id: str = "buildA", jobs: list[dict], total: int | None = None,
) -> dict:
    """Shape of the JSON Next.js embeds in ``__NEXT_DATA__``."""
    dso: dict = {"jobOffers": jobs, "searchId": "x"}
    if total is not None:
        dso["totalJobOfferCount"] = total
    return {
        "props": {
            "pageProps": {
                "currentPage": 1,
                "defaultSearchJobOfferData": dso,
            }
        },
        "page": "/search",
        "query": {},
        "buildId": build_id,
    }


def _listing_html(
    *, build_id: str = "buildA", jobs: list[dict], total: int | None = None,
) -> str:
    payload = _next_data(build_id=build_id, jobs=jobs, total=total)
    return (
        "<html><head></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def _data_response(jobs: list[dict], *, total: int | None = None) -> dict:
    """Shape of a ``_next/data/.../search.json`` response."""
    dso: dict = {"jobOffers": jobs}
    if total is not None:
        dso["totalJobOfferCount"] = total
    return {
        "pageProps": {
            "currentPage": 1,
            "defaultSearchJobOfferData": dso,
        }
    }


# --- registry ---------------------------------------------------------------


def test_registry_resolves_greenjapan() -> None:
    assert ScraperRegistry.get(ATSType.GREENJAPAN) is GreenJapanScraper


# --- happy path -------------------------------------------------------------


def test_discovers_build_id_and_walks_pages(httpx_mock) -> None:
    # 20 jobs in page 1 (HTML), 10 in page 2 (JSON) → total=30
    page1_jobs = [_job(id=i) for i in range(1, 21)]
    page2_jobs = [_job(id=i) for i in range(21, 31)]
    httpx_mock.add_response(
        url=_LISTING_URL,
        text=_listing_html(jobs=page1_jobs, total=30),
    )
    httpx_mock.add_response(
        url="https://www.green-japan.com/_next/data/buildA/search.json?page=2",
        json=_data_response(page2_jobs, total=30),
    )

    jobs = GreenJapanScraper("any").fetch()
    assert {int(j.ats_id) for j in jobs} == set(range(1, 31))


def test_maps_payload_fields_to_canonical_slots(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL,
        text=_listing_html(jobs=[_job()], total=1),
    )

    jobs = GreenJapanScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.GREENJAPAN
    assert j.ats_id == "235863"
    assert j.title == "システム開発エンジニア"
    assert j.company == "株式会社 D・Ace"
    assert j.country_iso == "JP"
    assert j.region == "Asia"
    assert j.language == "ja"
    assert j.location == "東京都, 神奈川県, 千葉県"
    assert str(j.url) == "https://www.green-japan.com/company/9939/job/235863"
    assert j.salary_summary == "410万円〜800万円"
    assert j.salary_currency == "JPY"
    assert j.salary_period == "YEAR"
    assert j.salary_min == 4_100_000.0
    assert j.salary_max == 8_000_000.0
    assert j.posted_at is not None
    assert j.raw is not None
    assert j.raw["skills"] == ["Python", "TypeScript", "Go"]
    assert j.raw["tags"] == ["リモート勤務の相談可"]
    assert j.raw["industry"] == "自由で効率的なシステム開発サービス"
    assert j.raw["company_id"] == 9939
    # headline is the marketing tagline distinct from the role name
    assert j.raw["headline"] == "賞与実績 5.2ヶ月分"


def test_uses_total_count_to_stop_pagination(httpx_mock) -> None:
    """``totalJobOfferCount`` is the source of truth — we shouldn't
    keep paging when we've already collected ``ceil(total/PER_PAGE)``
    pages."""
    httpx_mock.add_response(
        url=_LISTING_URL,
        text=_listing_html(jobs=[_job(id=1)], total=1),
    )
    # If the scraper tried page 2, httpx_mock would error out — there's
    # no stub for it. Total=1 with PER_PAGE=20 means one page.
    jobs = GreenJapanScraper("any").fetch()
    assert len(jobs) == 1


def test_max_pages_caps_walk(httpx_mock) -> None:
    """When the total would imply 5 pages but max_pages=2, we cap."""
    page1_jobs = [_job(id=i) for i in range(1, 21)]
    httpx_mock.add_response(
        url=_LISTING_URL,
        text=_listing_html(jobs=page1_jobs, total=100),
    )
    page2_jobs = [_job(id=i) for i in range(21, 41)]
    httpx_mock.add_response(
        url=_DATA_RE,
        json=_data_response(page2_jobs, total=100),
    )
    jobs = GreenJapanScraper("any", max_pages=2).fetch()
    assert len(jobs) == 40


# --- buildId rotation -------------------------------------------------------


def test_re_discovers_build_id_on_404(httpx_mock) -> None:
    page1_jobs = [_job(id=i) for i in range(1, 21)]
    httpx_mock.add_response(
        url=_LISTING_URL,
        text=_listing_html(build_id="buildA", jobs=page1_jobs, total=30),
    )
    # First page-2 call: 404 because the build rotated.
    httpx_mock.add_response(
        url="https://www.green-japan.com/_next/data/buildA/search.json?page=2",
        status_code=404,
    )
    # Re-discovery serves the new buildId.
    httpx_mock.add_response(
        url=_LISTING_URL,
        text=_listing_html(build_id="buildB", jobs=page1_jobs, total=30),
    )
    httpx_mock.add_response(
        url="https://www.green-japan.com/_next/data/buildB/search.json?page=2",
        json=_data_response(
            [_job(id=i) for i in range(21, 31)],
            total=30,
        ),
    )
    jobs = GreenJapanScraper("any").fetch()
    assert {int(j.ats_id) for j in jobs} == set(range(1, 31))


def test_missing_build_id_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL, text="<html>no nextjs here</html>",
    )
    with pytest.raises(ScraperError, match="buildId"):
        GreenJapanScraper("any").fetch()


def test_500_on_listing_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_URL, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        GreenJapanScraper("any").fetch()


# --- helpers ----------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("410万円〜800万円", (4_100_000.0, 8_000_000.0)),
    ("300〜600万円", (3_000_000.0, 6_000_000.0)),
    ("500万円以上", (5_000_000.0, None)),
    ("給与応相談", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_parse_jpy_salary(raw, expected) -> None:
    assert _parse_jpy_salary(raw) == expected


def test_parse_unix_timestamp_handles_ints() -> None:
    dt = _parse_unix_timestamp(1778459021)
    assert dt is not None
    assert dt.year >= 2026


def test_parse_unix_timestamp_rejects_garbage() -> None:
    assert _parse_unix_timestamp(0) is None
    assert _parse_unix_timestamp(None) is None
    assert _parse_unix_timestamp("not a timestamp") is None
