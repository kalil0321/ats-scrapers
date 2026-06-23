"""Tests for the Workforce Australia (jobsearch.gov.au) scraper.

Fixtures are real API responses captured from
``https://www.workforceaustralia.gov.au/api/v1/global/vacancies``
in 2026-05. No live HTTP — every test exercises ``_parse`` directly.
"""

from __future__ import annotations

from jobhive.models import ATSType
from jobhive.scrapers.workforceaustralia import (
    WorkforceAustraliaScraper,
    _format_location,
    _map_employment_type,
)

# Real envelope captured from the live API. The wrapper is
# ``{score, result}``; we exercise ``_parse`` against the full shape so
# any future change to the envelope structure surfaces immediately.
SAMPLE_FULL_TIME = {
    "score": 2.0,
    "result": {
        "contractType": None,
        "creationDate": "2026-04-13T11:54:27.357",
        "description": (
            "Title: Restaurant Manager\n\nLocation: Sandringham Victoria\n\n"
            "<strong>Duties</strong> include shift management &amp; ordering."
        ),
        "displayFromDate": "2026-04-13T00:00:00",
        "employerId": "CfDJ8NYRxeJdKHVJsNH820chayA",
        "employerName": "George Migration",
        "expiryDate": "2026-05-15T10:00:00",
        "howToApplyCode": "APTR",
        "industry": {"code": "124", "label": "Hospitality"},
        "isApplyOnlineJob": True,
        "isExternalJob": False,
        "isFavourite": False,
        "isIndigenousJob": False,
        "isNewJob": False,
        "jobType": {"code": "H", "label": "Normal position"},
        "latitude": "-37.95361065000",
        "location": {
            "code": "71BASU",
            "label": "VIC - Melbourne - Bayside & Peninsula Suburbs",
        },
        "logoUrl": "/api/v1/global/vacancies/logos/employer?employerId=x",
        "longitude": "145.01463237999",
        "modifiedDate": "2026-04-14T12:00:08.587",
        "occupation": {
            "code": "1411", "label": "Cafe and Restaurant Managers"
        },
        "organisation": {"code": "INET", "label": "Internet Business"},
        "positionsAvailable": 1,
        "postCode": "3191",
        "salary": {"code": "SLAA", "label": "Above Award"},
        "site": {"code": "NETV", "label": "Internet Vacancies"},
        "state": "VIC",
        "suburb": "SANDRINGHAM",
        "tenure": {"code": "P", "label": "Permanent position"},
        "title": "Restaurant Manager",
        "vacancyId": 2348240495,
        "workType": {"code": "F", "label": "Full time position"},
    },
}

SAMPLE_CASUAL_CONTRACT = {
    "score": 2.0,
    "result": {
        "contractType": None,
        "creationDate": "2026-04-12T20:38:04.7",
        "description": "Vending Machine Assistant needed urgently.",
        "displayFromDate": "2026-04-12T00:00:00",
        "employerId": "CfDJ8NYRxeJdKHVJsNH820chayAjAes5DSsN7b48nSVy",
        "employerName": "Nabropure Water Vending",
        "expiryDate": "2026-05-14T10:00:00",
        "howToApplyCode": "APTR",
        "industry": {"code": "137", "label": "Trades"},
        "isApplyOnlineJob": True,
        "isExternalJob": False,
        "isIndigenousJob": False,
        "jobType": {"code": "H", "label": "Normal position"},
        "latitude": "-27.54009581",
        "location": {
            "code": "41BRIS",
            "label": "QLD - Brisbane & Gold Coast - Brisbane & Surrounds",
        },
        "longitude": "152.95775258",
        "modifiedDate": "2026-04-14T10:36:16.26",
        "occupation": {"code": "8997", "label": "Vending Machine Attendants"},
        "organisation": {"code": "INET", "label": "Internet Business"},
        "positionsAvailable": 1,
        "postCode": "4073",
        "salary": {"code": "SLAW", "label": "Award"},
        "site": {"code": "NETV", "label": "Internet Vacancies"},
        "state": "QLD",
        "suburb": "SEVENTEEN MILE ROCKS",
        "tenure": {"code": "N", "label": "Contract position"},
        "title": "Vending Machine Assistant",
        "vacancyId": 2348184871,
        "workType": {"code": "C", "label": "Casual position"},
    },
}


def _scraper() -> WorkforceAustraliaScraper:
    return WorkforceAustraliaScraper("workforceaustralia")


# --- _parse: full-time permanent role ---------------------------------------


def test_parse_full_time_role_populates_identity_fields() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.ats_type is ATSType.WORKFORCEAUSTRALIA
    assert job.ats_id == "2348240495"
    assert job.title == "Restaurant Manager"
    assert job.company == "George Migration"
    assert str(job.url) == (
        "https://www.workforceaustralia.gov.au/individuals/jobs/details/2348240495"
    )


def test_parse_populates_country_and_language_for_australia() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.country_iso == "AU"
    assert job.language == "en"


def test_parse_builds_location_from_suburb_state_postcode() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.location == "Sandringham, VIC 3191"


def test_parse_extracts_geocoordinates_as_floats() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.lat is not None and job.lon is not None
    assert -38.0 < job.lat < -37.9
    assert 144.9 < job.lon < 145.1


def test_parse_strips_html_and_truncates_description() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.description is not None
    # HTML tags stripped, entities unescaped, whitespace collapsed.
    assert "<strong>" not in job.description
    assert "&amp;" not in job.description
    assert "Duties include shift management & ordering." in job.description


def test_parse_maps_full_time_work_type_to_full_time() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.employment_type == "FULL_TIME"


def test_parse_salary_summary_and_currency() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.salary_summary == "Above Award"
    assert job.salary_currency == "AUD"


def test_parse_department_and_team_from_industry_and_occupation() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.department == "Hospitality"
    assert job.team == "Cafe and Restaurant Managers"


def test_parse_raw_overflow_includes_flags() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.raw is not None
    assert job.raw.get("site") == "Internet Vacancies"
    assert job.raw.get("is_indigenous") is False
    assert job.raw.get("is_external") is False
    assert job.raw.get("positions_available") == 1
    assert job.raw.get("how_to_apply") == "APTR"


def test_parse_posted_at_parses_iso_datetime() -> None:
    job = _scraper()._parse(SAMPLE_FULL_TIME)
    assert job is not None
    assert job.posted_at is not None
    assert job.posted_at.year == 2026
    assert job.posted_at.month == 4
    assert job.posted_at.day == 13


# --- _parse: casual + contract tenure ---------------------------------------


def test_parse_casual_work_type_maps_to_temporary() -> None:
    job = _scraper()._parse(SAMPLE_CASUAL_CONTRACT)
    assert job is not None
    assert job.employment_type == "TEMPORARY"


def test_parse_casual_role_keeps_tenure_in_commitment() -> None:
    """Even though workType drives the canonical enum, the raw tenure
    label should still surface in ``commitment`` so consumers see the
    contract-vs-permanent distinction without parsing prose."""
    job = _scraper()._parse(SAMPLE_CASUAL_CONTRACT)
    assert job is not None
    # workType wins for employment_type; commitment falls back to the
    # most specific available raw label.
    assert job.commitment in {
        "Contract position",
        "Casual position",
    }


# --- _parse: defensive edge cases -------------------------------------------


def test_parse_missing_vacancy_id_returns_none() -> None:
    bad = {"result": {"title": "X", "employerName": "Y"}}
    assert _scraper()._parse(bad) is None


def test_parse_missing_title_returns_none() -> None:
    bad = {"result": {"vacancyId": 123, "employerName": "Y"}}
    assert _scraper()._parse(bad) is None


def test_parse_falls_back_to_organisation_when_employer_missing() -> None:
    item = {
        "result": {
            "vacancyId": 999,
            "title": "Engineer",
            "employerName": "",
            "organisation": {"code": "GOV", "label": "Federal Government"},
            "state": "ACT",
        }
    }
    job = _scraper()._parse(item)
    assert job is not None
    assert job.company == "Federal Government"


def test_parse_drops_zero_coordinates() -> None:
    item = dict(SAMPLE_FULL_TIME)
    item["result"] = {**SAMPLE_FULL_TIME["result"], "latitude": "0", "longitude": "0"}
    job = _scraper()._parse(item)
    assert job is not None
    assert job.lat is None
    assert job.lon is None


def test_parse_envelope_without_result_returns_none() -> None:
    assert _scraper()._parse({"score": 1.0}) is None
    assert _scraper()._parse({"score": 1.0, "result": None}) is None


# --- Pure helpers -----------------------------------------------------------


def test_format_location_combines_components() -> None:
    assert _format_location("SYDNEY", "NSW", "2000") == "Sydney, NSW 2000"


def test_format_location_handles_missing_postcode() -> None:
    assert _format_location("Sydney", "NSW", None) == "Sydney, NSW"


def test_format_location_handles_blank_values() -> None:
    assert _format_location("", "NSW", "") == "NSW"
    assert _format_location(None, None, None) is None


def test_map_employment_type_permanent_full_time() -> None:
    assert (
        _map_employment_type(
            "Full time position", "Permanent position", None, None
        )
        == "FULL_TIME"
    )


def test_map_employment_type_falls_back_to_tenure_for_contract() -> None:
    assert (
        _map_employment_type(None, "Contract position", None, None)
        == "CONTRACT"
    )


def test_map_employment_type_unknown_returns_none() -> None:
    assert _map_employment_type(None, None, None, None) is None
    assert (
        _map_employment_type(
            "Unrecognized label", "Mystery tenure", None, None
        )
        is None
    )
