"""Tests for the Foundit (Monster India / APAC) scraper.

Scope: country slug resolution, API row parsing (incl. structured
salary, employment-type mapping, posted-at epoch handling, ad-row
filtering), pagination + dedup, retry/backoff on 429/5xx, and
registry wiring. The httpx network path is exercised via
``pytest-httpx`` — we never hit the live site.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import FounditScraper, ScraperRegistry
from jobhive.scrapers import foundit as f_mod

# Per-country regex the httpx-mock matcher uses. We compile from the
# scraper's canonical domain map so adding a new country here doesn't
# silently miss the test.
_API_RE_BY_DOMAIN: dict[str, re.Pattern[str]] = {
    domain: re.compile(rf"^https://{re.escape(domain)}/middleware/jobsearch")
    for (domain, _t, _i, _r) in f_mod._COUNTRY_TABLE.values()
}
_INDIA_RE = _API_RE_BY_DOMAIN["www.foundit.in"]


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff sleeps add seconds per retry — patch to a no-op so the
    retry path doesn't cost wall-clock time."""
    monkeypatch.setattr(f_mod, "_sleep_backoff", lambda attempt: None)
    monkeypatch.setattr(f_mod, "MAX_RETRIES", 2)


def _make_row(
    *,
    job_id: str = "49286015",
    title: str = "Customer Service Executive",
    company: str = "Tech Mahindra Limited",
    locations: str = "Mumbai City, Mumbai",
    jd_url: str = "/job/customer-service-tech-mahindra-mumbai-49286015",
    currency: str = "INR",
    min_salary: int = 250_000,
    max_salary: int = 550_000,
    salary_summary: str = "2,50,000-5,50,000 INR",
    employment_types: list[str] | None = None,
    min_exp_years: int = 1,
    qualifications: list[str] | None = None,
    skills: str = "Customer Service, Sales, Communication",
    functions: list[str] | None = None,
    roles: list[str] | None = None,
    industry: str = "BPO/ITES",
    created_at_ms: int = 1_775_488_663_000,
    hide_salary: bool = False,
) -> dict[str, Any]:
    """One realistic Foundit API row. Mirrors the May 2026 payload."""
    return {
        "id": job_id,
        "jobId": int(job_id) if job_id.isdigit() else 0,
        "title": title,
        "companyName": company,
        "locations": locations,
        "jdUrl": jd_url,
        "currencyCode": currency,
        "minimumSalary": {
            "currency": currency,
            "absoluteValue": min_salary,
            "absoluteMonthlyValue": min_salary // 12,
        },
        "maximumSalary": {
            "currency": currency,
            "absoluteValue": max_salary,
            "absoluteMonthlyValue": max_salary // 12,
        },
        "salary": salary_summary,
        "hideSalary": 1 if hide_salary else 0,
        "employmentTypes": (
            employment_types if employment_types is not None else ["Full time"]
        ),
        "minimumExperience": {"years": min_exp_years},
        "maximumExperience": {"years": min_exp_years + 4},
        "qualifications": (
            qualifications if qualifications is not None else ["12th Class (XII)"]
        ),
        "skills": skills,
        "functions": (
            functions if functions is not None else ["Customer Service/Call Centre/BPO"]
        ),
        "roles": roles if roles is not None else ["Customer Service Executive"],
        "designations": ["Customer Service Executive"],
        "industry": industry,
        "exp": f"{min_exp_years}-{min_exp_years + 4} Years",
        "createdAt": created_at_ms,
        "channelName": "India",
        "isUrgentlyHiring": False,
        "kiwiJobId": "145448017",
        "jobTypes": [],
    }


def _make_page(
    rows: list[dict[str, Any]],
    *,
    total: int | None = None,
    next_cursor: str | None = None,
    previous_cursor: str | None = None,
) -> dict[str, Any]:
    """Wrap a list of rows in the canonical ``jobSearchStatus / response``
    envelope the scraper expects."""
    return {
        "jobSearchStatus": 200,
        "jobSearchStatusText": "OK",
        "jobSearchResponse": {
            "data": list(rows),
            "meta": {
                "paging": {
                    "cursors": {
                        "next": next_cursor or str(len(rows)),
                        "previous": previous_cursor or "0",
                    },
                    "total": total if total is not None else len(rows),
                    "limit": len(rows),
                },
                "resultId": "test-result-id",
                "searchId": "test-search-id",
                "version": "DEFAULT",
                "buildVersion": "0.0.1",
            },
            "spellCheckApplied": False,
            "filters": [],
            "selectedFilters": [],
            "filterLabels": [],
            "spellCheck": None,
            "apiId": "DEFAULT",
            "IS_NEW_SEARCH_PAGE": True,
        },
    }


# --- registry / construction ------------------------------------------


def test_registry_resolves_foundit() -> None:
    assert ScraperRegistry.get(ATSType.FOUNDIT) is FounditScraper


def test_default_country_slug_is_india() -> None:
    s = FounditScraper()
    assert s.country_slug == "in"
    assert s._country_iso == "IN"
    assert s._domain == "www.foundit.in"


@pytest.mark.parametrize(
    ("slug", "iso", "domain"),
    [
        ("in", "IN", "www.foundit.in"),
        ("sg", "SG", "www.foundit.sg"),
        ("my", "MY", "www.foundit.my"),
        ("id", "ID", "www.foundit.id"),
        ("ph", "PH", "www.foundit.com.ph"),
    ],
)
def test_known_country_slugs(slug: str, iso: str, domain: str) -> None:
    s = FounditScraper(slug)
    assert s._country_iso == iso
    assert s._domain == domain


@pytest.mark.parametrize(
    ("alias", "expected_slug"),
    [
        ("india", "in"),
        ("INDIA", "in"),
        ("singapore", "sg"),
        ("malaysia", "my"),
        ("indonesia", "id"),
        ("philippines", "ph"),
    ],
)
def test_country_aliases(alias: str, expected_slug: str) -> None:
    s = FounditScraper(alias)
    assert s.country_slug == expected_slug


def test_unknown_country_slug_raises() -> None:
    with pytest.raises(ScraperError, match="unknown country slug"):
        FounditScraper("atlantis")


# --- parsing ----------------------------------------------------------


def test_parses_minimal_realistic_row(httpx_mock: Any) -> None:
    """End-to-end parse of a single canonical row. Pins the field
    mapping the public dataset relies on."""
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([_make_row()]))
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.FOUNDIT
    assert j.ats_id == "49286015"
    assert j.global_id == "foundit:49286015"
    assert j.title == "Customer Service Executive"
    assert j.company == "Tech Mahindra Limited"
    assert str(j.url) == (
        "https://www.foundit.in/job/customer-service-tech-mahindra-mumbai-49286015"
    )
    assert j.location == "Mumbai City, Mumbai"
    assert j.country_iso == "IN"
    assert j.region == "Asia"
    assert j.language == "en"
    assert j.salary_currency == "INR"
    assert j.salary_period == "YEAR"
    assert j.salary_min == 250_000
    assert j.salary_max == 550_000
    assert j.salary_summary == "2,50,000-5,50,000 INR"
    assert j.experience == 1
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Full time"
    assert j.posted_at == datetime.fromtimestamp(1_775_488_663, tz=UTC)
    assert j.fetched_at is not None
    assert j.raw is not None
    assert j.raw["industry"] == "BPO/ITES"
    assert j.raw["experience_label"] == "1-5 Years"
    assert j.raw["education"] == ["12th Class (XII)"]
    assert j.raw["skills"] == ["Customer Service", "Sales", "Communication"]
    assert j.raw["functions"] == ["Customer Service/Call Centre/BPO"]
    assert j.raw["country_slug"] == "in"
    assert j.raw["channel_name"] == "India"
    assert j.raw["is_urgently_hiring"] is False
    assert j.raw["kiwi_job_id"] == "145448017"


def test_uses_country_domain_for_url(httpx_mock: Any) -> None:
    """When scraping Singapore, the absolute URL must reference
    ``www.foundit.sg`` — not the .in default. The public dataset
    stores the channel-specific URL so consumers can link back."""
    sg_re = _API_RE_BY_DOMAIN["www.foundit.sg"]
    httpx_mock.add_response(
        url=sg_re,
        json=_make_page([_make_row(jd_url="/job/sg-engineer-101", job_id="101")]),
    )
    httpx_mock.add_response(url=sg_re, json=_make_page([]))

    [job] = FounditScraper("sg", max_pages=5).fetch()
    assert str(job.url).startswith("https://www.foundit.sg/")
    assert job.country_iso == "SG"


def test_absolute_jd_url_is_preserved(httpx_mock: Any) -> None:
    """If the API ever switches to absolute URLs, we mustn't
    double-prefix the host."""
    abs_url = "https://www.foundit.in/job/external-42"
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(jd_url=abs_url, job_id="42")]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert str(job.url) == abs_url


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Full time", "FULL_TIME"),
        ("Permanent", "FULL_TIME"),
        ("Part time", "PART_TIME"),
        ("Contract", "CONTRACT"),
        ("Freelance", "CONTRACT"),
        ("Internship", "INTERN"),
        ("Trainee", "INTERN"),
        ("Temporary", "TEMPORARY"),
    ],
)
def test_employment_type_mapping(
    httpx_mock: Any, label: str, expected: str,
) -> None:
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(employment_types=[label])]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.employment_type == expected
    assert job.commitment == label


def test_employment_type_unknown_label_falls_back_to_none(
    httpx_mock: Any,
) -> None:
    """An unrecognised label leaves ``employment_type`` ``None`` —
    we don't fabricate a guess — but ``commitment`` keeps the raw
    string so downstream consumers can see the original."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(employment_types=["Apprenticeship-X"])]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.employment_type is None
    assert job.commitment == "Apprenticeship-X"


def test_employment_types_empty_list(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(employment_types=[])]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.employment_type is None
    assert job.commitment is None


def test_hide_salary_suppresses_compensation(httpx_mock: Any) -> None:
    """Confidential listings have ``hideSalary=1``. We honour the
    flag and skip the salary fields rather than leak values the
    employer chose not to publish."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(hide_salary=True)]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None
    assert job.salary_summary is None


def test_zero_salary_is_treated_as_missing(httpx_mock: Any) -> None:
    """Foundit encodes "not disclosed" as 0 in ``absoluteValue``.
    Treat as ``None`` so downstream filters / averages aren't
    poisoned by zero rows."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(min_salary=0, max_salary=0)]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.salary_min is None
    assert job.salary_max is None


def test_zero_experience_is_treated_as_missing(httpx_mock: Any) -> None:
    """Entry-level postings come with ``minimumExperience.years=0`` —
    that's not a valid lower bound, treat as missing."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(min_exp_years=0)]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.experience is None


def test_invalid_created_at_falls_back_to_none(httpx_mock: Any) -> None:
    """A garbage epoch must not crash the parse — ``posted_at``
    stays ``None`` and the row still ships."""
    row = _make_row(created_at_ms=0)
    row["createdAt"] = None  # null in the JSON payload
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([row]))
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.posted_at is None


def test_ad_placement_rows_are_skipped(httpx_mock: Any) -> None:
    """The API interleaves ``{"index": N, "type": "adsense"}`` /
    ``{"type": "banner"}`` entries — those have no ``id`` field and
    must be filtered out before parsing."""
    rows: list[dict[str, Any]] = [
        _make_row(job_id="1"),
        {"index": 0, "type": "adsense"},
        {"index": 0, "type": "banner"},
        _make_row(job_id="2"),
    ]
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page(rows))
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]


def test_skips_rows_missing_required_fields(httpx_mock: Any) -> None:
    """A row without title or jdUrl is a stub — skip cleanly rather
    than fabricate values."""
    valid = _make_row(job_id="1")
    no_title = _make_row(job_id="2", title="")
    no_jdurl = _make_row(job_id="3", jd_url="")
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([valid, no_title, no_jdurl]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_skips_row_with_no_id(httpx_mock: Any) -> None:
    """Defensive: a row with neither ``id`` nor ``jobId`` is dropped
    before reaching the parser. The page yields zero real rows so
    pagination stops (no second request needed)."""
    bogus = {"title": "no id", "jdUrl": "/job/x"}
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([bogus]))

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert jobs == []


def test_falls_back_to_job_id_when_id_missing(httpx_mock: Any) -> None:
    """The API typically populates both ``id`` and ``jobId`` — if only
    the numeric ``jobId`` is present, use it as ``ats_id``."""
    row = _make_row(job_id="123")
    row.pop("id")
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([row]))
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    [job] = FounditScraper("in", max_pages=5).fetch()
    assert job.ats_id == "123"


# --- pagination & dedup -----------------------------------------------


def test_paginates_until_empty_page(httpx_mock: Any) -> None:
    """Walks pages until the response yields zero rows."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="1")]),
    )
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="2")]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper("in", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]


def test_pagination_offsets_are_per_page_multiples(httpx_mock: Any) -> None:
    """Each request advances ``start`` by ``PER_PAGE`` — verify the
    URL pattern explicitly so a regression in the offset math is
    caught at the wire level."""
    for i in range(3):
        httpx_mock.add_response(
            url=_INDIA_RE,
            json=_make_page([_make_row(job_id=f"p{i}")]),
        )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    FounditScraper("in", max_pages=10).fetch()
    requests = httpx_mock.get_requests()
    assert len(requests) == 4
    starts = [r.url.params["start"] for r in requests]
    assert starts == ["0", "100", "200", "300"]


def test_dedupes_repeated_rows_across_pages(httpx_mock: Any) -> None:
    """Past the deep-pagination cap the API repeats the same window.
    The dedup-by-ats_id + zero-new-rows stop condition handles this
    without needing to know the exact cap."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="1"), _make_row(job_id="2")]),
    )
    # Page 2 returns the SAME rows — zero new == stop.
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="1"), _make_row(job_id="2")]),
    )

    jobs = FounditScraper("in", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]


def test_max_pages_caps_walk(httpx_mock: Any) -> None:
    """Honour the constructor cap — useful for smoke runs."""
    for i in range(3):
        httpx_mock.add_response(
            url=_INDIA_RE,
            json=_make_page([_make_row(job_id=f"p{i}")]),
        )

    jobs = FounditScraper("in", max_pages=3).fetch()
    assert len(jobs) == 3
    assert len(httpx_mock.get_requests()) == 3


def test_offset_cap_stops_walk_even_below_max_pages(
    httpx_mock: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard ``MAX_USABLE_OFFSET`` ceiling stops the loop before
    a very large ``max_pages`` would otherwise burn through requests
    against a wrap-around region."""
    monkeypatch.setattr(f_mod, "MAX_USABLE_OFFSET", 150)
    # MAX_USABLE_OFFSET=150 with PER_PAGE=100 → only starts 0 and 100
    # are issued; start=200 > 150 → break before request.
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="a")]),
    )
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="b")]),
    )

    FounditScraper("in", max_pages=99).fetch()
    starts = [r.url.params["start"] for r in httpx_mock.get_requests()]
    assert starts == ["0", "100"]


# --- retry / error handling -------------------------------------------


def test_retries_on_5xx_then_succeeds(httpx_mock: Any) -> None:
    httpx_mock.add_response(url=_INDIA_RE, status_code=502, text="bad gateway")
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="1")]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_retries_on_429_then_succeeds(httpx_mock: Any) -> None:
    httpx_mock.add_response(url=_INDIA_RE, status_code=429, text="rate limited")
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="1")]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_stops_pagination_after_exhausted_retries(
    httpx_mock: Any,
) -> None:
    """When the per-page retry budget is exhausted on a transient
    failure, the scraper stops pagination but returns whatever it
    already had — partial > nothing."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json=_make_page([_make_row(job_id="1")]),
    )
    # Page 2 always 502 — both retries fail.
    httpx_mock.add_response(url=_INDIA_RE, status_code=502)
    httpx_mock.add_response(url=_INDIA_RE, status_code=502)

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_4xx_other_than_429_stops_immediately(httpx_mock: Any) -> None:
    """A 400 / 404 isn't transient — surface a stop, don't retry."""
    httpx_mock.add_response(url=_INDIA_RE, status_code=400, text="bad request")

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert jobs == []


def test_non_json_200_stops_pagination(httpx_mock: Any) -> None:
    """A 200 that doesn't parse as JSON shouldn't crash the scraper —
    log a warning, stop walking, return what we have."""
    httpx_mock.add_response(url=_INDIA_RE, status_code=200, text="<html>oops</html>")

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert jobs == []


def test_api_level_error_status_stops_pagination(httpx_mock: Any) -> None:
    """The envelope's own ``jobSearchStatus != 200`` signals an
    application-level error (e.g. a bad ``country`` token). Stop and
    return what we have."""
    httpx_mock.add_response(
        url=_INDIA_RE,
        json={
            "jobSearchStatus": 400,
            "jobSearchStatusText": "Bad Request",
            "jobSearchResponse": {},
        },
    )

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert jobs == []


# --- request shape ---------------------------------------------------


def test_request_carries_required_params(httpx_mock: Any) -> None:
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    FounditScraper("in", max_pages=1).fetch()
    [req] = httpx_mock.get_requests()
    qs = req.url.params
    assert qs["sort"] == "1"
    assert qs["limit"] == "100"
    assert qs["query"] == ""
    assert qs["searchId"] == ""
    assert qs["queryDerived"] == "true"
    assert qs["country"] == "india"
    assert qs["start"] == "0"


def test_request_carries_browserlike_headers(httpx_mock: Any) -> None:
    """The middleware endpoint accepts any UA in May 2026 but we
    mirror Chrome to blend in if the WAF tightens."""
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    FounditScraper("in", max_pages=1).fetch()
    [req] = httpx_mock.get_requests()
    assert req.headers["Accept"] == "application/json"
    assert req.headers["Referer"] == "https://www.foundit.in/"
    assert "Chrome" in req.headers["User-Agent"]


def test_singapore_uses_singapore_referer_and_country(
    httpx_mock: Any,
) -> None:
    sg_re = _API_RE_BY_DOMAIN["www.foundit.sg"]
    httpx_mock.add_response(url=sg_re, json=_make_page([]))

    FounditScraper("sg", max_pages=1).fetch()
    [req] = httpx_mock.get_requests()
    assert req.url.params["country"] == "singapore"
    assert req.headers["Referer"] == "https://www.foundit.sg/"


# --- module-level helper coverage ------------------------------------


def test_split_skills_strips_and_filters_empties() -> None:
    out = f_mod._split_skills(" Java , , Python ,SQL")
    assert out == ["Java", "Python", "SQL"]


def test_split_skills_non_string_returns_empty() -> None:
    assert f_mod._split_skills(None) == []
    assert f_mod._split_skills(["Java"]) == []


def test_nested_amount_handles_missing_field() -> None:
    assert f_mod._nested_amount(None) is None
    assert f_mod._nested_amount({"currency": "INR"}) is None
    assert f_mod._nested_amount({"absoluteValue": "100"}) is None


def test_epoch_ms_to_dt_roundtrips() -> None:
    out = f_mod._epoch_ms_to_dt(1_775_488_663_000)
    assert out == datetime.fromtimestamp(1_775_488_663, tz=UTC)


def test_epoch_ms_to_dt_rejects_garbage() -> None:
    assert f_mod._epoch_ms_to_dt(None) is None
    assert f_mod._epoch_ms_to_dt("nope") is None
    assert f_mod._epoch_ms_to_dt(-1) is None


def test_str_or_none_int_to_str() -> None:
    """``id`` arrives as a string in current payloads, but ``jobId`` is
    numeric — the helper must round-trip ints to canonical strings."""
    assert f_mod._str_or_none(49286015) == "49286015"
    assert f_mod._str_or_none("  abc  ") == "abc"
    assert f_mod._str_or_none("") is None
    assert f_mod._str_or_none(None) is None
