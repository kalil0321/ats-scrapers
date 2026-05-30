"""Tests for the Wantedly (Japan) scraper.

Pin the parsing contract (each Wantedly project field → Job field) and
the page-numbered pagination behaviour. ``_metadata.total_pages`` drives
termination — the suite mirrors that.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, WantedlyScraper

_API_RE = re.compile(r"^https://www\.wantedly\.com/api/v1/projects")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.wantedly as w
    monkeypatch.setattr(w, "MAX_RETRIES", 1)
    monkeypatch.setattr(w, "RETRY_BASE_DELAY", 0.0)


def _project(
    *,
    project_id: int = 2261400,
    title: str = "スタートアップでの開発に興味のあるフルスタックエンジニア募集！",
    company_name: str = "Acme",
    company_slug: str | None = "acme",
    location: str | None = "東京都中央区日本橋茅場町１−８−１",
    location_suffix: str | None = "茅場町一丁目平和ビル７階",
    description: str = "▍募集背景 教育体制は完全内製化しており",
    looking_for: str = "勤務地条件なし｜フルリモート",
    published_at: str = "2026-05-08T17:16:29.611+09:00",
    tags: list[dict[str, Any]] | None = None,
    support_count: int = 42,
    page_view: int = 1234,
    candidate_count: int = 5,
) -> dict[str, Any]:
    company: dict[str, Any] = {"id": 999, "name": company_name}
    if company_slug is not None:
        company["slug"] = company_slug
    return {
        "id": project_id,
        "title": title,
        "published_at": published_at,
        "support_count": support_count,
        "page_view": page_view,
        "candidate_count": candidate_count,
        "location": location,
        "location_suffix": location_suffix,
        "description": description,
        "looking_for": looking_for,
        "company": company,
        "tags": tags if tags is not None else [{"id": 1, "name": "Python"}],
    }


def _page(
    items: list[dict[str, Any]],
    *,
    page: int = 1,
    total_objects: int = 1,
    total_pages: int = 1,
) -> dict[str, Any]:
    return {
        "data": items,
        "_metadata": {
            "total_objects": total_objects,
            "per_page": 10,
            "current_page": page,
            "total_pages": total_pages,
        },
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_wantedly() -> None:
    assert ScraperRegistry.get(ATSType.WANTEDLY) is WantedlyScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_project_payload(httpx_mock) -> None:
    """Single-page scrape with a Japanese-language project; verify every
    populated Job field maps to the right Wantedly field."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_project()]),
    )

    jobs = WantedlyScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.WANTEDLY
    assert j.ats_id == "2261400"
    assert j.title.startswith("スタートアップ")
    assert j.company == "Acme"
    assert str(j.url) == "https://www.wantedly.com/projects/2261400"
    # Location combines location + location_suffix with a comma.
    assert j.location is not None
    assert "東京都中央区" in j.location
    assert "茅場町一丁目平和ビル" in j.location
    assert ", " in j.location
    # Japanese title → country_iso=JP, language=ja.
    assert j.country_iso == "JP"
    assert j.language == "ja"
    # Description concatenates description + looking_for.
    assert j.description is not None
    assert "募集背景" in j.description
    assert "勤務地条件なし" in j.description
    # First tag becomes department; full tag list lives in raw.
    assert j.department == "Python"
    assert j.raw is not None
    assert j.raw["tags"] == ["Python"]
    assert j.raw["company_slug"] == "acme"
    assert j.raw["support_count"] == 42
    assert j.raw["page_view"] == 1234
    assert j.raw["candidate_count"] == 5
    # ISO datetime with +09:00 offset parses.
    assert j.posted_at is not None
    assert j.posted_at.year == 2026
    assert j.posted_at.month == 5
    assert j.fetched_at is not None


# --- language / country heuristics ------------------------------------------


def test_english_title_yields_language_en(httpx_mock) -> None:
    """Some JP-based listings use an English title — language=en, but
    location still JP-shaped so country_iso stays JP."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _project(
                project_id=1,
                title="Software Engineer @ AcmeCorp",
                location="東京",
                location_suffix=None,
            )
        ]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert jobs[0].language == "en"
    assert jobs[0].country_iso == "JP"


def test_non_jp_location_strips_country_iso(httpx_mock) -> None:
    """A Wantedly listing pointing at Singapore should leave country_iso
    as None so downstream enrichment can resolve the actual country."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _project(
                project_id=2,
                title="Engineering Manager",
                location="Singapore",
                location_suffix=None,
            )
        ]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert jobs[0].country_iso is None
    assert jobs[0].language == "en"


def test_us_country_code_strips_country_iso(httpx_mock) -> None:
    """ISO alpha-2 ``US`` as a standalone token strips country_iso back
    to None."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _project(
                project_id=3,
                title="Backend Engineer",
                location="San Francisco, US",
                location_suffix=None,
            )
        ]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert jobs[0].country_iso is None


def test_no_location_defaults_to_jp(httpx_mock) -> None:
    """Empty location is the common case for fully-remote JP postings.
    Default country_iso=JP — Wantedly is overwhelmingly Japanese."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _project(
                project_id=4,
                title="Remote Job",
                location=None,
                location_suffix=None,
            )
        ]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert jobs[0].country_iso == "JP"
    assert jobs[0].location is None


# --- description ------------------------------------------------------------


def test_strips_html_from_description(httpx_mock) -> None:
    """``description`` may contain HTML — must be stripped to plain text."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _project(
                project_id=5,
                description="<p>Hello <b>world</b></p>",
                looking_for="<br>looking for <i>engineers</i>",
            )
        ]),
    )
    jobs = WantedlyScraper("any").fetch()
    desc = jobs[0].description
    assert desc is not None
    assert "<" not in desc and ">" not in desc
    assert "Hello world" in desc
    assert "looking for engineers" in desc


def test_description_truncated_to_10k(httpx_mock) -> None:
    long_body = "x" * 20_000
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _project(project_id=6, description=long_body, looking_for="")
        ]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert jobs[0].description is not None
    assert len(jobs[0].description) <= 10_000


# --- pagination -------------------------------------------------------------


def test_paginates_via_total_pages(httpx_mock) -> None:
    """First page exposes ``_metadata.total_pages``; the worker walks
    pages 2..N concurrently and merges."""
    httpx_mock.add_response(
        url=re.compile(r".*page=1(&|$).*"),
        json=_page(
            [_project(project_id=1, title="Job 1")],
            page=1,
            total_objects=3,
            total_pages=3,
        ),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=2(&|$).*"),
        json=_page(
            [_project(project_id=2, title="Job 2")],
            page=2,
            total_objects=3,
            total_pages=3,
        ),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=3(&|$).*"),
        json=_page(
            [_project(project_id=3, title="Job 3")],
            page=3,
            total_objects=3,
            total_pages=3,
        ),
    )

    jobs = WantedlyScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2", "3"}


def test_max_pages_caps_walk(httpx_mock) -> None:
    """When ``_metadata.total_pages`` exceeds ``max_pages``, the walk
    stops at the cap."""
    httpx_mock.add_response(
        url=re.compile(r".*page=1(&|$).*"),
        json=_page(
            [_project(project_id=1, title="Job 1")],
            page=1,
            total_objects=1000,
            total_pages=100,
        ),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=2(&|$).*"),
        json=_page(
            [_project(project_id=2, title="Job 2")],
            page=2,
            total_objects=1000,
            total_pages=100,
        ),
    )

    jobs = WantedlyScraper("any", max_pages=2).fetch()
    assert {j.ats_id for j in jobs} == {"1", "2"}


def test_dedupes_repeated_ids_across_pages(httpx_mock) -> None:
    """If two pages echo the same project id, dedupe on ats_id."""
    httpx_mock.add_response(
        url=re.compile(r".*page=1(&|$).*"),
        json=_page(
            [_project(project_id=1), _project(project_id=2)],
            page=1,
            total_objects=3,
            total_pages=2,
        ),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=2(&|$).*"),
        json=_page(
            [_project(project_id=2), _project(project_id=3)],
            page=2,
            total_objects=3,
            total_pages=2,
        ),
    )
    jobs = WantedlyScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2", "3"}


# --- field handling ---------------------------------------------------------


def test_skips_projects_missing_id_or_title(httpx_mock) -> None:
    """Defensive: drop malformed entries rather than emitting broken rows."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _project(project_id=1, title="Good"),
            {"id": 2, "company": {"name": "Acme"}},  # no title
            {"title": "No id", "company": {"name": "Acme"}},
        ]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_handles_missing_company_slug(httpx_mock) -> None:
    """The current API serializer ships ``company.id`` but not ``slug``.
    Scraper must not crash — raw.company_slug just goes to None."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_project(project_id=7, company_slug=None)]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert jobs[0].raw is not None
    assert jobs[0].raw["company_slug"] is None


def test_empty_tags_yields_no_department(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_project(project_id=8, tags=[])]),
    )
    jobs = WantedlyScraper("any").fetch()
    assert jobs[0].department is None
    assert jobs[0].raw is not None
    assert jobs[0].raw["tags"] == []


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    """Real server failures should surface, not silently emit []."""
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        WantedlyScraper("any").fetch()


def test_404_on_overflow_page_returns_empty_slice(httpx_mock) -> None:
    """Wantedly may 404 past the end of the dataset — treat as empty."""
    httpx_mock.add_response(
        url=re.compile(r".*page=1(&|$).*"),
        json=_page(
            [_project(project_id=1, title="Job 1")],
            page=1,
            total_objects=2,
            total_pages=2,
        ),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=2(&|$).*"),
        status_code=404,
    )
    jobs = WantedlyScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]
