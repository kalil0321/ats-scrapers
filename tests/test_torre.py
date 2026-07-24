"""Tests for the Torre.co (LATAM + global remote) scraper.

Torre exposes a public POST search API with cursor-based pagination.
These tests pin the parsing contract for the canonical ``Job`` row and
exercise the cursor-following pagination loop. No live HTTP — fixtures
below are trimmed from real probe responses (2026-05-12).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, TorreScraper

# pytest-httpx matches on URL path; matching with a regex keeps the test
# tolerant of query-string ordering (httpx serializes ``params={}`` dicts
# in insertion order, but the order isn't part of the contract).
_SEARCH_RE = re.compile(r"^https://search\.torre\.co/opportunities/_search/")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.torre as torre

    monkeypatch.setattr(torre, "MAX_RETRIES", 1)
    monkeypatch.setattr(torre, "RETRY_BASE_DELAY", 0.0)


# --- fixture builders -------------------------------------------------------


def _opp_to_be_agreed() -> dict[str, Any]:
    """Real-world shape: ``compensation.data.code == "to-be-agreed"``,
    min/max amounts both 0. Must NOT populate salary fields (treating 0
    as a real bound would corrupt the dataset)."""
    return {
        "id": "Yd6mya1w",
        "objective": "Asesor Profesional de Seguros e Inversiones",
        "slug": "grupo-cuatro-vidas-asesor-profesional-de-seguros-e-inversiones",
        "tagline": "Gestionarás capital y diseñarás estrategias.",
        "type": "full-time-employment",
        "opportunity": "employee",
        "organizations": [
            {
                "id": 2923370,
                "hashedId": "RZ3Lj7ro",
                "name": "Grupo Cuatro Vidas",
                "publicId": "GrupoCuatroVidas",
                "size": 15,
            }
        ],
        "locations": ["Querétaro, Qro., México"],
        "remote": True,
        "external": False,
        "deadline": None,
        "created": "2025-04-30T16:42:17.000Z",
        "status": "open",
        "commitment": "full-time",
        "compensation": {
            "data": {
                "code": "to-be-agreed",
                "currency": "MXN",
                "minAmount": 0.0,
                "minHourlyUSD": 0.0,
                "maxAmount": 0.0,
                "maxHourlyUSD": 0.0,
                "periodicity": "monthly",
                "negotiable": False,
            },
            "visible": True,
            "additionalCompensationDetails": {"comissions": "65000"},
        },
        "skills": [
            {"name": "Customer care", "proficiency": "novice"},
            {"name": "Sales", "proficiency": "novice"},
            {"name": "Insurance sales", "proficiency": "no-experience-interested"},
        ],
        "place": {
            "remote": True,
            "anywhere": False,
            "locationType": "hybrid",
            "location": [
                {
                    "id": "Querétaro, Qro., México",
                    "countryCode": "MX",
                    "latitude": 20.5887932,
                    "longitude": -100.3898881,
                }
            ],
        },
    }


def _opp_with_salary_range() -> dict[str, Any]:
    """Real range: ``code == "range"``, min/max populated, currency USD."""
    return {
        "id": "arQneQnW",
        "objective": "Mac User Research Participant",
        "slug": "lusauto-mac-user-research-participant",
        "tagline": "Contribuirás al éxito global en autopartes.",
        "type": "full-time-employment",
        "opportunity": "employee",
        "organizations": [
            {"id": 870801, "name": "Lusauto", "publicId": "Lusauto", "size": 100}
        ],
        "locations": ["Perú"],
        "remote": True,
        "external": False,
        "deadline": "2026-06-08T14:01:21.000Z",
        "created": "2026-05-09T14:01:21.000Z",
        "status": "open",
        "commitment": "part-time",
        "compensation": {
            "data": {
                "code": "range",
                "currency": "USD",
                "minAmount": 500.0,
                "maxAmount": 1000.0,
                "periodicity": "monthly",
                "negotiable": False,
            },
            "visible": True,
            "additionalCompensationDetails": {},
        },
        "skills": [
            {"name": "Office automation"},
            {"name": "Software development"},
        ],
        "place": {
            "remote": True,
            "locationType": "remote_countries",
            "location": [
                {
                    "id": "Perú",
                    "countryCode": "PE",
                    "latitude": -9.189967,
                    "longitude": -75.015152,
                }
            ],
        },
    }


def _opp_no_compensation() -> dict[str, Any]:
    """``compensation.data`` is None when the employer hid the salary."""
    return {
        "id": "8wDqNZOd",
        "objective": "Arquitecto de Soluciones Plataforma",
        "slug": "acme-arquitecto",
        "tagline": "Design platform architectures.",
        "type": "full-time-employment",
        "opportunity": "employee",
        "organizations": [{"id": 1, "name": "Acme", "publicId": "Acme"}],
        "locations": ["Colombia"],
        "remote": True,
        "created": "2025-12-01T00:00:00.000Z",
        "status": "open",
        "commitment": "full-time",
        "compensation": {
            "data": None,
            "visible": False,
            "additionalCompensationDetails": {},
        },
        "skills": [],
        "place": {
            "remote": True,
            "location": [
                {"id": "Bogotá, Colombia", "countryCode": "CO"}
            ],
        },
    }


def _envelope(results: list[dict[str, Any]], *, next_cursor: str | None) -> dict:
    return {
        "total": 200534,
        "size": len(results),
        "offset": 0,
        "results": results,
        "pagination": {
            "previous": None,
            "next": next_cursor,
        },
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_torre() -> None:
    assert ScraperRegistry.get(ATSType.TORRE) is TorreScraper


def test_scraper_ats_attribute() -> None:
    assert TorreScraper("any").ats is ATSType.TORRE


# --- happy path: structured-range salary ------------------------------------


def test_parses_full_opportunity_with_salary_range(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_SEARCH_RE,
        json=_envelope([_opp_with_salary_range()], next_cursor=None),
    )

    jobs = TorreScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]

    assert j.ats_type is ATSType.TORRE
    assert j.ats_id == "arQneQnW"
    assert j.title == "Mac User Research Participant"
    assert j.company == "Lusauto"
    assert str(j.url) == (
        "https://torre.ai/postings/arQneQnW/lusauto-mac-user-research-participant"
    )
    # place.location → location + country + lat/lon
    assert j.location == "Perú"
    assert j.country_iso == "PE"
    assert j.lat == pytest.approx(-9.189967)
    assert j.lon == pytest.approx(-75.015152)
    # remote flag: only ever set True per project convention
    assert j.is_remote is True
    # Structured salary
    assert j.salary_currency == "USD"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 500.0
    assert j.salary_max == 1000.0
    assert j.salary_summary is not None
    assert "500" in j.salary_summary and "1,000" in j.salary_summary
    assert "USD" in j.salary_summary
    # Employment type: part-time → PART_TIME, raw commitment preserved
    assert j.employment_type == "PART_TIME"
    assert j.commitment == "part-time"
    # Description: tagline + bulleted skills list
    assert j.description is not None
    assert "Contribuirás al éxito global" in j.description
    assert "- Office automation" in j.description
    assert "- Software development" in j.description
    # Posted_at parsed from ISO 8601
    assert j.posted_at is not None
    assert j.posted_at.year == 2026
    # raw overflow
    assert j.raw is not None
    assert j.raw.get("organization_public_id") == "Lusauto"
    assert j.raw.get("organization_size") == 100
    assert j.raw.get("type") == "full-time-employment"
    assert j.raw.get("opportunity") == "employee"
    assert j.raw.get("external") is False
    assert "deadline" in j.raw
    assert j.raw.get("skills") == ["Office automation", "Software development"]


def test_include_descriptions_false_omits_description(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_SEARCH_RE,
        json=_envelope([_opp_with_salary_range()], next_cursor=None),
    )

    job = TorreScraper("any", include_descriptions=False).fetch()[0]

    assert job.description is None


# --- to-be-agreed: salary must stay None ------------------------------------


def test_to_be_agreed_yields_no_salary_fields(httpx_mock) -> None:
    """``code == "to-be-agreed"`` ships with ``minAmount=0``, ``maxAmount=0``.
    Treating these as real bounds would let nonsense ``0 MXN`` rows leak
    into the dataset. The currency must NOT be set either when both
    bounds are empty (per JOB_SCHEMA.md: ``salary_currency`` is set
    iff the ATS exposes a real range)."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        json=_envelope([_opp_to_be_agreed()], next_cursor=None),
    )

    jobs = TorreScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.salary_min is None
    assert j.salary_max is None
    assert j.salary_currency is None
    assert j.salary_period is None
    assert j.salary_summary is None
    # but the rest of the row still parses:
    assert j.title == "Asesor Profesional de Seguros e Inversiones"
    assert j.country_iso == "MX"
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "full-time"


def test_null_compensation_data_yields_no_salary_fields(httpx_mock) -> None:
    """``compensation.data: None`` is the "employer hid it" case. Treat
    identically to "no salary at all"."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        json=_envelope([_opp_no_compensation()], next_cursor=None),
    )

    j = TorreScraper("any").fetch()[0]
    assert j.salary_currency is None
    assert j.salary_min is None
    assert j.salary_max is None
    # other fields still parse:
    assert j.country_iso == "CO"
    assert j.location == "Bogotá, Colombia"


# --- pagination -------------------------------------------------------------


def test_follows_cursor_pagination(httpx_mock) -> None:
    """Two pages: first response has ``pagination.next`` set, second is
    null → loop exits. Both opportunities should appear in the result."""
    httpx_mock.add_response(
        url=re.compile(rf"{_SEARCH_RE.pattern}(?!.*after=)"),
        json=_envelope([_opp_with_salary_range()], next_cursor="CURSOR_PAGE_2"),
    )
    httpx_mock.add_response(
        url=re.compile(rf"{_SEARCH_RE.pattern}.*after=CURSOR_PAGE_2"),
        json=_envelope([_opp_to_be_agreed()], next_cursor=None),
    )

    jobs = TorreScraper("any").fetch()
    assert len(jobs) == 2
    assert {j.ats_id for j in jobs} == {"arQneQnW", "Yd6mya1w"}


def test_dedups_repeated_ids_across_pages(httpx_mock) -> None:
    """Defensive: if the server somehow yields the same opportunity on
    two consecutive pages (cursor edge cases), we de-duplicate by
    ``ats_id`` before adding to the output list."""
    httpx_mock.add_response(
        url=re.compile(rf"{_SEARCH_RE.pattern}(?!.*after=)"),
        json=_envelope([_opp_with_salary_range()], next_cursor="C2"),
    )
    httpx_mock.add_response(
        url=re.compile(rf"{_SEARCH_RE.pattern}.*after=C2"),
        # Same id appears again.
        json=_envelope([_opp_with_salary_range()], next_cursor=None),
    )
    jobs = TorreScraper("any").fetch()
    assert len(jobs) == 1


def test_stops_on_empty_results(httpx_mock) -> None:
    """An empty ``results`` list terminates pagination even if a cursor
    is set (defensive against the server returning an empty trailing
    page)."""
    httpx_mock.add_response(
        url=_SEARCH_RE, json=_envelope([], next_cursor="SHOULD_NOT_FOLLOW")
    )
    jobs = TorreScraper("any").fetch()
    assert jobs == []


# --- error handling ---------------------------------------------------------


def test_500_response_raises_scraper_error(httpx_mock) -> None:
    httpx_mock.add_response(url=_SEARCH_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        TorreScraper("any").fetch()


def test_400_response_raises_scraper_error(httpx_mock) -> None:
    """When the server rejects the page-size (``too large``) it returns
    400 with a JSON body. The scraper currently fixes ``PAGE_SIZE`` to
    something well under the cap, but a future bump should surface as
    a hard error rather than silent zero-results."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        status_code=400,
        json={"meta": {"message": "Request size too large"}},
    )
    with pytest.raises(ScraperError):
        TorreScraper("any").fetch()


def test_non_object_json_raises_scraper_error(httpx_mock) -> None:
    httpx_mock.add_response(url=_SEARCH_RE, json=[])
    with pytest.raises(ScraperError, match="expected object"):
        TorreScraper("any").fetch()
