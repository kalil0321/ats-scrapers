"""Tests for the CATS careers-portal scraper."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import CatsOneScraper, ScraperRegistry
from ats_scrapers.scrapers import catsone as module
from ats_scrapers.scrapers.catsone import _date, _decode, _loader_data, _parse_slug

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py.

BASE = "https://acme.catsone.com"
PORTAL_URL = f"{BASE}/careers/"
PORTAL = "7701"


def _encode(value: object) -> list[Any]:
    """Write a value the way the pages do: one flat array, objects as
    ``{"_<index of key>": <index of value>}``, arrays as lists of indexes,
    -5 for a missing value, ``["D", ms]`` for a date."""
    flat: list[Any] = []

    def add(item: object) -> int:
        if item is None:
            return -5
        index = len(flat)
        flat.append(None)
        if isinstance(item, dict):
            flat[index] = {f"_{add(key)}": add(val) for key, val in item.items()}
        elif isinstance(item, list):
            flat[index] = [add(val) for val in item]
        elif isinstance(item, datetime):
            flat[index] = ["D", int(item.timestamp() * 1000)]
        else:
            flat[index] = item
        return index

    add(value)
    return flat


def _page(loader_data: dict[str, Any]) -> str:
    payload = json.dumps(_encode({"loaderData": loader_data, "actionData": None}))
    return (
        "<html><head><title>Careers | Acme Staffing</title></head><body>"
        '<div id="root"><a class="table-row" href="/careers/7701/jobs/1">Rendered</a></div>'
        "<script>window.__reactRouterContext.streamController.enqueue("
        f"{json.dumps(payload)});</script>"
        "<script>window.__reactRouterContext.streamController.close();</script>"
        "</body></html>"
    )


def _portal(**overrides: object) -> dict[str, Any]:
    portal: dict[str, Any] = {
        "portalId": PORTAL,
        "parentPortalId": None,
        "name": "Acme Staffing Inc.",
        "active": True,
        "siteUrl": "https://acme.example",
    }
    portal.update(overrides)
    return portal


def _row(number: int, title: str = "Millwright", **overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "jobId": str(number),
        "positionTitle": title,
        "city": "Windsor",
        "state": "ON",
        "zip": "N8W 5K5",
        "latitude": None,
        "longitude": None,
        "summary": "Acme is hiring.",
        "datePosted": "2026-10-16T07:34:42.000Z",
        "dateCreated": "2026-10-16T12:35:38.890Z",
        "dateModified": "2026-11-06T15:56:05.000Z",
        "fields": {
            "city": "Windsor",
            "state": "ON",
            "branch": "Xerox Canada Ltd",
            "country": "Canada",
            "jobType": "contract",
            "duration": "12 months",
            "location": "Windsor, ON",
        },
    }
    fields = overrides.pop("fields", None)
    row.update(overrides)
    if isinstance(fields, dict):
        row["fields"] = {**row["fields"], **fields}
    return row


def _portal_page(rows: list[Any], **portal: object) -> str:
    return _page(
        {
            "portals/cats-careers/portal": {"portal": _portal(**portal), "requestUrl": "x"},
            "portals/cats-careers/jobs": {"jobs": rows, "searchFields": {}, "job": None},
        }
    )


def _job_page(number: int, **job: object) -> str:
    detail: dict[str, Any] = {
        "jobId": str(number),
        "positionTitle": "Millwright",
        "descriptionHtml": "<p>Maintain &amp; repair <b>machines</b>.</p>",
        "datePosted": datetime(2026, 10, 21, 15, 56, 13, tzinfo=UTC),
        "country": "Canada",
        "countryCode": "CA",
        "stateCode": None,
    }
    detail.update(job)
    return _page(
        {
            "portals/cats-careers/portal": {"portal": _portal()},
            "portals/cats-careers/job": {"job": detail, "similarJobs": []},
        }
    )


def _serve(httpx_mock, rows: list[Any], url: str = PORTAL_URL, **portal: object) -> None:
    httpx_mock.add_response(url=url, text=_portal_page(rows, **portal), is_reusable=True)


def _serve_job(httpx_mock, number: int, **job: object) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/careers/{PORTAL}/jobs/{number}",
        text=_job_page(number, **job),
        is_reusable=True,
    )


def _scraper(slug: str = "acme", **options: object) -> CatsOneScraper:
    options.setdefault("include_descriptions", False)
    return CatsOneScraper(slug, **options)


# --- Registry and slug ------------------------------------------------------


def test_registry_resolves_catsone() -> None:
    assert ScraperRegistry.get(ATSType.CATSONE) is CatsOneScraper


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("acme", ("acme", None)),
        (" ACME ", ("acme", None)),
        ("acme.catsone.com", ("acme", None)),
        ("acme/7701", ("acme", "7701")),
        ("acme.catsone.com/7701", ("acme", "7701")),
        ("https://acme.catsone.com", ("acme", None)),
        ("https://acme.catsone.com/careers/", ("acme", None)),
        ("https://ACME.catsone.com/careers/7701-General", ("acme", "7701")),
        ("https://acme.catsone.com/careers/7701/jobs/16866544-Millwright", ("acme", "7701")),
    ],
)
def test_slug_forms(slug: str, expected: tuple[str, str | None]) -> None:
    assert _parse_slug(slug) == expected
    scraper = CatsOneScraper(slug)
    assert (scraper.tenant, scraper.portal) == expected
    assert scraper.base_url == BASE


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "   ",
        "-acme",
        "acme-",
        "acme corp",
        "ac\nme",
        "acme/abc",
        "acme/77/01",
        "acme.example.com",
        "a.b.catsone.com",
        "http://acme.catsone.com/careers/",
        "https://catsone.com/careers/",
        "https://a.b.catsone.com/careers/",
        "https://acme.catsone.com.evil.com/careers/",
        "https://evil.com/acme.catsone.com",
        "https://user:pw@acme.catsone.com/careers/",
        "https://acme.catsone.com:8443/careers/",
    ],
)
def test_invalid_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(ScraperError):
        CatsOneScraper(slug)


# --- Portal page ------------------------------------------------------------


def test_parses_a_portal(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [
            _row(16864772, "Account Support Rep (Part-Time)", fields={"salary": "$25.78/hour"}),
            _row(16866544, "Marketing Coordinator"),
        ],
    )

    jobs = _scraper().fetch()

    assert [job.title for job in jobs] == [
        "Account Support Rep (Part-Time)",
        "Marketing Coordinator",
    ]
    first = jobs[0]
    assert str(first.url) == f"{BASE}/careers/7701/jobs/16864772"
    assert first.company == "Acme Staffing Inc."
    assert first.ats_type is ATSType.CATSONE
    assert first.ats_id == "16864772"
    assert first.global_id == "catsone:16864772"
    assert first.location == "Windsor, ON, Canada"
    assert first.posted_at == datetime(2026, 10, 16, 7, 34, 42, tzinfo=UTC)
    assert first.salary_summary == "$25.78/hour"
    assert first.commitment == "contract"
    assert first.employment_type == "CONTRACT"
    assert first.is_remote is None
    assert first.description is None
    assert first.raw == {
        "portal": "7701",
        "branch": "Xerox Canada Ltd",
        "duration": "12 months",
        "date_modified": "2026-11-06T15:56:05.000Z",
    }
    assert jobs[1].salary_summary is None


def test_company_is_the_given_name_then_the_portal_name_then_the_tenant(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)])

    assert _scraper(company_name=" Acme Group ").fetch()[0].company == "Acme Group"
    assert _scraper(company_name="  ").fetch()[0].company == "Acme Staffing Inc."

    httpx_mock.reset()
    _serve(httpx_mock, [_row(1)], name=None)

    assert _scraper().fetch()[0].company == "acme"


def test_a_portal_number_in_the_slug_is_the_page_read(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)], url=f"{BASE}/careers/8802", portalId="8802")

    job = _scraper("acme/8802").fetch()[0]

    assert str(job.url) == f"{BASE}/careers/8802/jobs/1"
    assert job.raw["portal"] == "8802"


@pytest.mark.parametrize(
    ("kind", "commitment", "employment_type"),
    [
        ("hire", "hire", None),
        ("contract", "contract", "CONTRACT"),
        ("contract-to-hire", "contract-to-hire", "CONTRACT"),
        ("Contract", "Contract", "CONTRACT"),
        (None, None, None),
    ],
)
def test_kind_of_hire(
    httpx_mock, kind: str | None, commitment: str | None, employment_type: str | None
) -> None:
    _serve(httpx_mock, [_row(1, fields={"jobType": kind})])

    job = _scraper().fetch()[0]

    assert job.commitment == commitment
    assert job.employment_type == employment_type


@pytest.mark.parametrize(
    ("remote_type", "is_remote"),
    [("Fully remote", True), ("Hybrid Remote", None), ("COVID-19", None), (None, None)],
)
def test_only_a_fully_remote_job_is_remote(
    httpx_mock, remote_type: str | None, is_remote: bool | None
) -> None:
    _serve(httpx_mock, [_row(1, fields={"remoteType": remote_type})])

    job = _scraper().fetch()[0]

    assert job.is_remote is is_remote
    assert job.raw.get("remote_type") == remote_type


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({}, "Windsor, ON, Canada"),
        ({"city": None}, "ON, Canada"),
        ({"state": " ", "fields": {"country": None}}, "Windsor"),
        ({"city": None, "state": None}, "Windsor, ON, Canada"),
        ({"city": None, "state": None, "fields": {"location": None}}, "Canada"),
        ({"city": None, "state": None, "fields": {"location": None, "country": None}}, None),
    ],
)
def test_location(httpx_mock, row: dict[str, Any], expected: str | None) -> None:
    _serve(httpx_mock, [_row(1, **row)])

    assert _scraper().fetch()[0].location == expected


def test_an_empty_portal_returns_no_job(httpx_mock) -> None:
    _serve(httpx_mock, [])

    assert _scraper().fetch() == []


def test_a_job_listed_twice_is_kept_once(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1), _row(1, "Again"), _row(2)])

    assert [job.ats_id for job in _scraper().fetch()] == ["1", "2"]


def test_an_unreadable_job_counts_against_the_floor(httpx_mock) -> None:
    unreadable = [
        _row(10, jobId=None),
        "not a job",
    ]
    _serve(httpx_mock, [_row(n) for n in range(1, 10)] + unreadable[:1])

    assert len(_scraper().fetch()) == 9

    httpx_mock.reset()
    _serve(httpx_mock, [_row(n) for n in range(1, 9)] + unreadable)

    with pytest.raises(ScraperError, match="only 8 of the 10 jobs"):
        _scraper().fetch()


@pytest.mark.parametrize(
    "row", [{"jobId": "16-abc"}, {"jobId": ""}, {"positionTitle": "  "}, {"positionTitle": None}]
)
def test_a_job_without_a_number_or_a_title_is_not_read(httpx_mock, row: dict[str, Any]) -> None:
    _serve(httpx_mock, [_row(1, **row)])

    with pytest.raises(ScraperError, match="only 0 of the 1 jobs"):
        _scraper().fetch()


def test_a_portal_that_says_it_is_not_active_is_not_found(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)], active=False)

    with pytest.raises(CompanyNotFoundError, match="portal 7701 is not active"):
        _scraper().fetch()


def test_a_missing_portal_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        _scraper().fetch()


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ("<html><body>Maintenance</body></html>", "carries no portal data"),
        (
            "<script>window.__reactRouterContext.streamController.enqueue(notAString);</script>",
            "carries no portal data",
        ),
        (
            '<script>window.__reactRouterContext.streamController.enqueue("not json");</script>',
            "not valid JSON",
        ),
        (
            '<script>window.__reactRouterContext.streamController.enqueue("[]");</script>',
            "not a list of values",
        ),
        (_page({}), "not a careers portal"),
        (_page({"portals/cats-careers/portal": {"portal": {"portalId": "abc"}}}), "not a careers"),
        (_page({"portals/cats-careers/portal": {"portal": _portal()}}), "no job list"),
        (
            _page(
                {
                    "portals/cats-careers/portal": {"portal": _portal()},
                    "portals/cats-careers/jobs": {"jobs": {"1": "x"}},
                }
            ),
            "no job list",
        ),
    ],
)
def test_a_page_that_is_not_a_portal_is_an_error(httpx_mock, page: str, message: str) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=page)

    with pytest.raises(ScraperError, match=message):
        _scraper().fetch()


def test_data_sent_in_several_pieces_is_put_together() -> None:
    payload = json.dumps(_encode({"loaderData": {"route": {"value": "x" * 50}}}))
    cut = len(payload) // 2
    page = "".join(
        f"<script>window.__reactRouterContext.streamController.enqueue({json.dumps(part)});</script>"
        for part in (payload[:cut], payload[cut:] + '\nP3:[{"_1":2},"late","value"]')
    )

    assert _loader_data(page) == {"route": {"value": "x" * 50}}


# --- Pages as the portals serve them ----------------------------------------

# The argument of ``streamController.enqueue(...)`` of two pages of one
# portal (visionpool.catsone.com, three jobs), captured on 13 Nov 2026: the
# portal page and the page of its job 16872462. They are kept as served,
# except three identifiers the tests do not need (the captcha site key and
# two form numbers), replaced by zeros of the same length.
FIXTURES = Path(__file__).parent / "fixtures" / "catsone"
REAL = "https://visionpool.catsone.com"


def _captured(name: str) -> str:
    argument = (FIXTURES / name).read_text(encoding="utf-8")
    return (
        "<html><body><script>window.__reactRouterContext.streamController.enqueue("
        f"{argument});</script></body></html>"
    )


def test_a_captured_portal_page_is_read(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{REAL}/careers/", text=_captured("portal_enqueue_argument.txt")
    )

    jobs = CatsOneScraper("visionpool", include_descriptions=False).fetch()

    assert [(job.ats_id, job.title) for job in jobs] == [
        ("16872952", "Senior Project Manager"),
        ("16872462", "Senior AI Solutions Lead"),
        ("16872450", "Senior Development, Maintenance, and Support (.NET) Consultant"),
    ]
    job = jobs[1]
    assert str(job.url) == f"{REAL}/careers/78002/jobs/16872462"
    assert job.company == "Visionpool Business Services"
    assert job.location == "Regina, SK, Canada"
    assert job.posted_at == datetime(2026, 11, 9, 15, 43, 8, tzinfo=UTC)
    assert job.commitment == "hire"
    assert job.employment_type is None
    assert job.raw == {
        "portal": "78002",
        "branch": "SaskEnergy",
        "date_modified": "2026-11-12T16:32:52.000Z",
    }


def test_a_captured_job_page_is_read(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{REAL}/careers/", text=_captured("portal_enqueue_argument.txt")
    )
    httpx_mock.add_response(
        url=f"{REAL}/careers/78002/jobs/16872462", text=_captured("job_enqueue_argument.txt")
    )
    scraper = CatsOneScraper("visionpool", include_descriptions=False)
    job = scraper.fetch()[1]

    description = scraper.get_description(job)

    assert description is not None
    assert "Senior AI Solutions Lead" in description
    assert "<p>" not in description
    assert len(description) > 2000
    assert job.country_iso == "CA"


def test_the_captured_job_page_gives_its_date_as_a_typed_value() -> None:
    data = _loader_data(_captured("job_enqueue_argument.txt"))

    detail = data["portals/cats-careers/job"]["job"]
    assert detail["jobId"] == "16872462"
    assert detail["datePosted"] == datetime(2026, 11, 9, 15, 43, 8, tzinfo=UTC)
    assert detail["countryCode"] == "CA"
    assert detail["recruiterSignatureHtml"] is None


# --- Decoding ---------------------------------------------------------------


def test_decode_rebuilds_objects_arrays_and_missing_values() -> None:
    value = {"a": [1, "two", None, {"b": True}], "c": 2.5, "d": None}

    assert _decode(_encode(value)) == value


def test_decode_reads_a_date() -> None:
    moment = datetime(2026, 10, 21, 15, 56, 13, tzinfo=UTC)

    assert _decode(_encode({"when": moment})) == {"when": moment}
    assert _decode([{"_1": 2}, "when", ["D", "soon"]]) == {"when": None}
    # The other typed values are not needed.
    assert _decode([{"_1": 2}, "p", ["P", 4]]) == {"p": None}


def test_decode_keeps_values_shared_by_several_holders_and_cycles() -> None:
    # 0: {"self": <0>, "list": <2>, "again": <2>}; 2: [<0>]
    flat = [{"_1": 0, "_3": 2, "_4": 2}, "self", [0], "list", "again"]

    root = _decode(flat)

    assert root["self"] is root
    assert root["list"] is root["again"]
    assert root["list"][0] is root


@pytest.mark.parametrize(
    "flat",
    [
        None,
        {},
        [],
        [{"_1": 9}, "key"],
        [{"_9": 1}, "value"],
        [{"key": 1}, "value"],
        [{"_1": 2}, 5, "value"],
        [{"_1": "2"}, "key", "value"],
        [[1.5], "value"],
        [[True], "value"],
    ],
)
def test_decode_refuses_data_it_does_not_understand(flat: object) -> None:
    with pytest.raises(ValueError, match="portal data"):
        _decode(flat)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-16T07:34:42.000Z", datetime(2026, 10, 16, 7, 34, 42, tzinfo=UTC)),
        ("2026-10-16T07:34:42", datetime(2026, 10, 16, 7, 34, 42, tzinfo=UTC)),
        (datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        ("16/10/2026", None),
        ("", None),
        (None, None),
        (1792598173000, None),
    ],
)
def test_date(value: object, expected: datetime | None) -> None:
    assert _date(value) == expected


# --- Job page ---------------------------------------------------------------


def test_the_job_page_gives_the_description_and_the_country(httpx_mock) -> None:
    _serve(httpx_mock, [_row(16866544)])
    _serve_job(httpx_mock, 16866544)

    job = _scraper(include_descriptions=True).fetch()[0]

    # Plain text, as everywhere in the dataset.
    assert job.description == "Maintain & repair machines."
    assert job.country_iso == "CA"
    # The date of the list stands.
    assert job.posted_at == datetime(2026, 10, 16, 7, 34, 42, tzinfo=UTC)


def test_the_job_page_gives_the_date_when_the_list_has_none(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1, datePosted=None)])
    _serve_job(httpx_mock, 1)

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.posted_at == datetime(2026, 10, 21, 15, 56, 13, tzinfo=UTC)


@pytest.mark.parametrize("code", ["CAN", "", None, "1A", 12])
def test_a_country_code_that_is_not_two_letters_is_not_kept(httpx_mock, code: object) -> None:
    _serve(httpx_mock, [_row(1)])
    _serve_job(httpx_mock, 1, countryCode=code)

    assert _scraper(include_descriptions=True).fetch()[0].country_iso is None


def test_the_description_keeps_one_line_per_block(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)])
    _serve_job(
        httpx_mock,
        1,
        descriptionHtml=(
            "<p>Duties:</p><ul><li>Weld</li><li>Fit &amp; grind</li></ul>"
            "<pre>Mon  08:00\nTue  09:00</pre><script>track()</script>"
        ),
    )

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description == "Duties:\nWeld\nFit & grind\nMon 08:00\nTue 09:00"


@pytest.mark.parametrize("markup", ["", "   ", "<p> </p>", None, 12])
def test_a_description_without_text_is_no_description(httpx_mock, markup: object) -> None:
    _serve(httpx_mock, [_row(1)])
    _serve_job(httpx_mock, 1, descriptionHtml=markup)

    assert _scraper(include_descriptions=True).fetch()[0].description is None


def test_a_job_page_that_shows_another_job_is_not_used(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)])
    httpx_mock.add_response(url=f"{BASE}/careers/{PORTAL}/jobs/1", text=_job_page(2))

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description is None
    assert job.country_iso is None


def test_a_job_page_that_fails_keeps_the_job(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1), _row(2, "Welder"), _row(3, "Fitter")])
    httpx_mock.add_response(
        url=f"{BASE}/careers/{PORTAL}/jobs/1", status_code=500, is_reusable=True
    )
    httpx_mock.add_response(url=f"{BASE}/careers/{PORTAL}/jobs/2", text="<html>Gone</html>")
    _serve_job(httpx_mock, 3, descriptionHtml="<p>Fits.</p>")

    jobs = _scraper(include_descriptions=True).fetch()

    assert [job.description for job in jobs] == [None, None, "Fits."]


def test_the_description_is_capped(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_DESCRIPTION_CHARS", 20)
    _serve(httpx_mock, [_row(1)])
    _serve_job(httpx_mock, 1, descriptionHtml="<p>" + "word " * 40 + "</p>")

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description == "word word word word"


def test_get_description_fetches_one_job(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)])
    _serve_job(httpx_mock, 1, descriptionHtml="<p>Welds.</p>")
    scraper = _scraper()
    job = scraper.fetch()[0]
    assert job.description is None

    assert scraper.get_description(job) == "Welds."
    assert scraper.get_description(job) == "Welds."
    assert len([r for r in httpx_mock.get_requests() if r.url.path.endswith("/jobs/1")]) == 1
