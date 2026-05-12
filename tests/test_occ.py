"""Tests for the OCC Mundial (Mexico) scraper.

Scope: registry wiring, httpcloak gating (graceful degradation when
the optional TLS-impersonation client is missing), Cloudflare
challenge detection + preset rotation, Apollo state parsing
(``__NEXT_DATA__`` extraction, ``ROOT_QUERY → jobsByUrl`` ordering,
direct fallback when ROOT_QUERY is absent), field mapping pinning
(URL canonicalisation, salary, employment type, work mode,
confidential employer fallback), pagination dedup, and retry/backoff
behaviour. The httpcloak network path is exercised via a stub
session — we never hit the live site in tests.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from jobhive.models import ATSType
from jobhive.scrapers import OCCMexicoScraper, ScraperRegistry
from jobhive.scrapers import occ as occ_mod

# --- Stub session --------------------------------------------------------


class _StubResponse:
    """Mimics ``httpcloak.Response`` enough for the scraper's needs."""

    def __init__(self, *, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _StubSession:
    """Stand-in for ``httpcloak.Session``. Records every GET so tests
    can assert on URL + headers, then replays canned responses in
    order. The scraper doesn't use ``Session`` as a context manager
    (it keeps the session alive across pages and calls ``close()``
    in a finally) so we mirror that flat call shape.
    """

    def __init__(self, responses: list[_StubResponse]) -> None:
        self._responses = list(responses)
        self.gets: list[dict[str, Any]] = []
        self.closed = False

    def get(
        self, url: str, *, headers: dict, timeout: float,
    ) -> _StubResponse:
        self.gets.append(
            {"url": url, "headers": headers, "timeout": timeout},
        )
        if not self._responses:
            raise AssertionError("ran out of stubbed responses")
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff sleeps add seconds per retry — patch to a no-op so
    retry-path tests don't pay wall-clock cost."""
    monkeypatch.setattr(occ_mod, "_sleep_backoff", lambda attempt: None)


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_StubResponse],
) -> _StubSession:
    """Pretend ``httpcloak.Session(preset=...)`` returns our stub.

    The scraper iterates over multiple presets in
    ``_open_session_and_first_page``; we surface a *single* session
    so the test scopes one preset at a time. The scraper's
    contract is "first non-CF preset wins" so the canned page 1
    response decides whether preset rotation happens.
    """
    session = _StubSession(responses)
    sessions_created: list[_StubSession] = []

    class _StubHttpCloak:
        HTTPCloakError = RuntimeError

        @staticmethod
        def Session(preset: str) -> _StubSession:  # noqa: N802 - mimic API
            session._preset = preset  # type: ignore[attr-defined]
            sessions_created.append(session)
            return session

    monkeypatch.setattr(occ_mod, "_httpcloak_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "httpcloak", _StubHttpCloak)
    session._sessions_created = sessions_created  # type: ignore[attr-defined]
    return session


def _install_session_factory(
    monkeypatch: pytest.MonkeyPatch,
    responses_per_preset: list[list[_StubResponse]],
) -> list[_StubSession]:
    """Mounts a stub ``httpcloak`` that hands out a *different*
    session per ``Session(preset=...)`` call. Used to test the
    Cloudflare-preset-rotation path: the first N sessions return CF
    challenges, the N+1th clears, and the scraper continues with the
    cleared session.
    """
    sessions = [_StubSession(r) for r in responses_per_preset]
    counter = {"idx": 0}

    class _StubHttpCloak:
        HTTPCloakError = RuntimeError

        @staticmethod
        def Session(preset: str) -> _StubSession:  # noqa: N802
            i = counter["idx"]
            if i >= len(sessions):
                raise AssertionError(
                    f"more presets tried ({i + 1}) than stubbed "
                    f"({len(sessions)})"
                )
            session = sessions[i]
            session._preset = preset  # type: ignore[attr-defined]
            counter["idx"] += 1
            return session

    monkeypatch.setattr(occ_mod, "_httpcloak_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "httpcloak", _StubHttpCloak)
    return sessions


# --- HTML / Apollo fixtures -------------------------------------------


def _job_entry(
    *,
    job_id: str = "18087589",
    title: str = "Analista de precios unitarios residente de obra",
    description: str | None = (
        "Importante corporativo solicita: Analista de Precios Unitarios."
    ),
    friendly_id: str | None = None,
    company_name: str = "Robert Bosch, S.A. de C.V.",
    company_pretty: str = "Robert Bosch",
    confidential: bool = False,
    location_desc: str = "Álvaro Obregón, Ciudad de México",
    state_abbr: str | None = "CDMX",
    city_name: str | None = "Álvaro Obregón",
    salary_show: bool = False,
    salary_from: int = 0,
    salary_to: int = 0,
    full_time: bool = True,
    part_time: bool = False,
    contract: bool = False,
    permanent: bool = True,
    temporary: bool = False,
    work_mode: str = "IN_PERSON",
    publish_date: str | None = "2024-02-21 00:00:00",
    job_type: str = "PREMIUM",
    category_id: str | None = "5",
    rank: int = 1,
    url_path: str | None = None,
) -> dict[str, Any]:
    """One realistic Apollo ``Job`` entry. Mirrors the shape captured
    from a 2024 Wayback snapshot of ``/empleos/`` — exhaustively
    spelled out so each test can override the field it cares about."""
    fid = friendly_id or f"{job_id}-some-job-slug"
    return {
        "__typename": "Job",
        "id": job_id,
        "url": url_path or (
            f"/empleo/oferta/{fid}"
            "?rank=1&page=1&sessionid=abc-def&userid=&uuid=xyz"
            "&utm_origin=web&utm_channel=premium"
        ),
        "title": title,
        "status": "ACTIVE",
        "description": description,
        "jobType": job_type,
        "salary": {
            "__typename": "JobSalary",
            "show": salary_show,
            "from": salary_from,
            "to": salary_to,
            "time": 0,
            "performanceCompensation": 0,
            "variableCompensation": 0,
        },
        "location": {
            "__typename": "JobLocation",
            "description": location_desc,
            "locations": [
                {
                    "__typename": "JobLocationData",
                    "city": {
                        "__typename": "CityLocation",
                        "description": city_name or "",
                        "jobCity": city_name or "",
                    },
                    "country": {"__ref": "CountryLocation:MX"},
                    "state": {
                        "__typename": "StateLocation",
                        "description": "Ciudad de México",
                        "abbreviation": state_abbr or "",
                    },
                },
            ],
        },
        "hiring": {
            "__typename": "JobHiring",
            "contract": contract,
            "fullTime": full_time,
            "partTime": part_time,
            "permanent": permanent,
            "temporary": temporary,
        },
        "workMode": {
            "__typename": "JobWorkMode",
            "description": work_mode,
        },
        "company": {
            "__typename": "JobCompany",
            "name": company_name,
            "namePretty": company_pretty,
            "confidential": confidential,
        },
        "profileId": "1234-some-profile",
        "category": (
            {"__ref": f"JobCategory:{category_id}"} if category_id else None
        ),
        "subcategory": {"__ref": "JobSubcategory:62"},
        "dates": {
            "__typename": "JobDates",
            "active": "2024-02-14 18:15:46",
            "publish": publish_date or "",
            "expires": "2024-04-14 23:59:59",
        },
        "friendlyId": fid,
        "rank": rank,
    }


def _wrap_page(
    entries: list[dict[str, Any]],
    *,
    include_root_query: bool = True,
) -> str:
    """Wrap a list of Apollo ``Job`` entries in just enough HTML +
    ``__NEXT_DATA__`` to mirror what OCC serves. Setting
    ``include_root_query=False`` exercises the fallback path where
    the scraper walks ``initialApolloState`` directly because
    ``ROOT_QUERY → jobsByUrl(...)`` is missing or empty."""
    apollo: dict[str, Any] = {}
    if include_root_query and entries:
        apollo["ROOT_QUERY"] = {
            "__typename": "Query",
            "jobsByUrl({\"channel\":\"serp\",\"url\":\"/empleos/\"})": {
                "__typename": "JobsResponse",
                "jobList": [
                    {"__ref": f"Job:{e['id']}"} for e in entries
                ],
            },
        }
    for entry in entries:
        apollo[f"Job:{entry['id']}"] = entry

    next_data = {
        "props": {
            "pageProps": {
                "initialApolloState": apollo,
                "isLoggedSSR": False,
            },
            "__N_SSP": True,
        },
        "page": "/empleos/[[...slug]]",
        "query": {},
        "buildId": "test",
    }
    body = json.dumps(next_data, ensure_ascii=False)
    return (
        '<!DOCTYPE html><html lang="es"><head><title>Empleos OCC</title>'
        '</head><body><main>job listings</main>'
        f'<script id="__NEXT_DATA__" type="application/json">{body}'
        '</script></body></html>'
    )


_CLOUDFLARE_PAGE = (
    '<!DOCTYPE html><html lang="en-US"><head>'
    '<title>Just a moment...</title>'
    '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
    '</head><body>cf-challenge</body></html>'
)


# --- Registry / construction ------------------------------------------


def test_registry_resolves_occ() -> None:
    """The OCC scraper must be discoverable through the registry —
    pipelines look it up by ``ATSType.OCC``, not by direct import."""
    assert ScraperRegistry.get(ATSType.OCC) is OCCMexicoScraper


def test_default_slug_is_all() -> None:
    """No slug argument means "scrape the entire MX feed" — the
    base ``/empleos`` URL, no path segment."""
    s = OCCMexicoScraper()
    assert s.slug == "all"


def test_empty_slug_falls_back_to_default() -> None:
    """An empty/whitespace slug is treated the same as ``"all"`` —
    callers shouldn't have to special-case "no filter" vs
    ``None``."""
    assert OCCMexicoScraper("").slug == "all"
    assert OCCMexicoScraper("   ").slug == "all"
    assert OCCMexicoScraper(None).slug == "all"  # type: ignore[arg-type]


def test_slug_strips_surrounding_slashes() -> None:
    """Callers occasionally include ``/`` on the slug. Normalize so
    URL building doesn't double up or drop the path segment."""
    assert OCCMexicoScraper("/en-jalisco/").slug == "en-jalisco"
    assert OCCMexicoScraper("en-jalisco").slug == "en-jalisco"


def test_build_url_first_page_default_slug() -> None:
    """The unfiltered MX-wide listing lives at ``/empleos`` — no
    trailing slash, no query string."""
    s = OCCMexicoScraper("all")
    assert s._build_url(1) == "https://www.occ.com.mx/empleos"


def test_build_url_first_page_named_slug() -> None:
    """Named slugs resolve to ``/empleos/{slug}/`` with a trailing
    slash. OCC redirects without it but we save the round-trip."""
    s = OCCMexicoScraper("en-jalisco")
    assert s._build_url(1) == (
        "https://www.occ.com.mx/empleos/en-jalisco/"
    )


def test_build_url_subsequent_pages() -> None:
    """``page=2`` appended via ``?`` for the unfiltered feed, ``&``
    when the slug already carries a query (none of OCC's known
    slugs do, but be defensive)."""
    s = OCCMexicoScraper("all")
    assert s._build_url(2) == "https://www.occ.com.mx/empleos?page=2"
    s = OCCMexicoScraper("en-jalisco")
    assert s._build_url(3) == (
        "https://www.occ.com.mx/empleos/en-jalisco/?page=3"
    )


# --- Graceful degradation --------------------------------------------


def test_returns_empty_with_warning_when_httpcloak_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``httpcloak`` isn't installed, log a warning and return
    ``[]`` so the publish pipeline keeps moving — same contract as
    Bayt / Kariyer / Tesla."""
    monkeypatch.setattr(occ_mod, "_httpcloak_available", lambda: False)
    with caplog.at_level(logging.WARNING):
        jobs = OCCMexicoScraper().fetch()
    assert jobs == []
    assert any(
        "httpcloak required" in r.getMessage().lower() for r in caplog.records
    )


def test_all_presets_blocked_returns_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When every TLS preset is blocked by Cloudflare we don't crash
    the publish pipeline — log a warning, return ``[]``."""
    # Six presets, every one returns a 403 CF challenge.
    presets = len(occ_mod._HTTPCLOAK_PRESETS)
    _install_session_factory(
        monkeypatch,
        [
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)]
            for _ in range(presets)
        ],
    )
    with caplog.at_level(logging.WARNING):
        jobs = OCCMexicoScraper().fetch()
    assert jobs == []
    assert any(
        "all" in r.getMessage().lower()
        and "preset" in r.getMessage().lower()
        for r in caplog.records
    )


# --- Apollo parsing ----------------------------------------------------


def test_parses_minimal_realistic_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end parse of a single Apollo ``Job`` entry. Pins the
    field mapping the public dataset relies on."""
    page = _wrap_page([_job_entry()])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )

    jobs = OCCMexicoScraper("all", max_pages=5).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.OCC
    assert j.ats_id == "18087589"
    assert j.title == "Analista de precios unitarios residente de obra"
    # The tracking query string on the in-Apollo url is stripped;
    # the canonical URL uses the friendlyId.
    assert str(j.url) == (
        "https://www.occ.com.mx/empleo/oferta/18087589-some-job-slug/"
    )
    # ``namePretty`` wins over ``name`` when both are present —
    # ``Robert Bosch`` is the consumer-facing rendering OCC uses.
    assert j.company == "Robert Bosch"
    assert j.country_iso == "MX"
    assert j.region == "North America"
    assert j.language == "es"
    assert j.location == "Álvaro Obregón, Ciudad de México"
    # IN_PERSON → is_remote=False (explicit signal from OCC).
    assert j.is_remote is False
    # No salary surfaced when ``show: False``.
    assert j.salary_currency is None
    assert j.salary_min is None
    assert j.salary_max is None
    # Full-time + permanent → FULL_TIME with the Spanish labels
    # preserved in ``commitment``.
    assert j.employment_type == "FULL_TIME"
    assert j.commitment is not None
    assert "Tiempo completo" in j.commitment
    assert "Permanente" in j.commitment
    # Publish date parsed as UTC (OCC reports CDMX-local without TZ).
    assert j.posted_at == datetime(2024, 2, 21, 0, 0, 0, tzinfo=UTC)
    assert j.fetched_at is not None
    assert j.description == (
        "Importante corporativo solicita: Analista de Precios Unitarios."
    )
    assert j.raw is not None
    assert j.raw["category"] == "5"
    assert j.raw["subcategory"] == "62"
    assert j.raw["work_mode"] == "IN_PERSON"
    assert j.raw["state"] == "CDMX"
    assert j.raw["city"] == "Álvaro Obregón"
    assert j.raw["job_type"] == "PREMIUM"
    assert j.raw["is_confidential"] is False


def test_canonical_url_strips_tracking_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCC's in-Apollo ``url`` includes ``?rank=&sessionid=&uuid=`` —
    per-impression noise. The canonical URL we store must be stable
    across scrapes so dedup works."""
    entry = _job_entry(
        url_path=(
            "/empleo/oferta/12345-engineer"
            "?rank=42&sessionid=zzz&uuid=qqq&page=7"
        ),
        friendly_id="12345-engineer",
        job_id="12345",
    )
    page = _wrap_page([entry])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert str(job.url) == (
        "https://www.occ.com.mx/empleo/oferta/12345-engineer/"
    )
    assert "sessionid" not in str(job.url)
    assert "rank" not in str(job.url)


def test_confidential_employer_uses_fallback_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the employer is confidential, OCC ships
    ``company.name = "Empresa confidencial"`` and
    ``namePretty = ""``. We must surface a non-empty company name —
    falling back to the Spanish label keeps the ``Job.company``
    field required-but-meaningful."""
    page = _wrap_page(
        [
            _job_entry(
                job_id="999",
                company_name="Empresa confidencial",
                company_pretty="",
                confidential=True,
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.company == "Empresa confidencial"
    assert job.raw is not None
    assert job.raw["is_confidential"] is True


def test_salary_surfaced_when_show_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postings with ``salary.show = True`` ship a structured MXN
    range. Map to canonical fields + emit a Spanish summary the
    consumer can render verbatim."""
    page = _wrap_page(
        [
            _job_entry(
                salary_show=True,
                salary_from=11347,
                salary_to=14062,
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.salary_currency == "MXN"
    assert job.salary_period == "MONTH"
    assert job.salary_min == 11347
    assert job.salary_max == 14062
    assert job.salary_summary == "$11,347 - $14,062 MXN"


def test_salary_single_value_emits_clean_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OCC ships only a ``from`` (no ``to``), the summary uses
    a single-value rendering rather than ``$X - $0 MXN``."""
    page = _wrap_page(
        [
            _job_entry(
                salary_show=True,
                salary_from=20000,
                salary_to=0,
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.salary_min == 20000
    assert job.salary_max is None
    assert job.salary_summary == "$20,000 MXN"


def test_salary_hidden_zero_range_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show: True`` but both bounds zero is treated the same as a
    hidden range — don't fabricate a $0 MXN summary."""
    page = _wrap_page(
        [
            _job_entry(
                salary_show=True,
                salary_from=0,
                salary_to=0,
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.salary_currency is None
    assert job.salary_summary is None


@pytest.mark.parametrize(
    ("mode", "expected_remote"),
    [
        ("IN_PERSON", False),
        ("REMOTE", True),
        ("HYBRID", None),
        ("", None),
    ],
)
def test_work_mode_to_is_remote(
    mode: str,
    expected_remote: bool | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``IN_PERSON`` → ``is_remote=False``; ``REMOTE`` → ``True``;
    ``HYBRID`` / unknown → ``None`` so downstream enrichment can
    decide based on the description."""
    page = _wrap_page([_job_entry(work_mode=mode, job_id="m-" + mode)])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.is_remote is expected_remote


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (
            {"fullTime": True, "permanent": True},
            "FULL_TIME",
        ),
        (
            {"partTime": True, "permanent": True},
            "PART_TIME",
        ),
        (
            {"contract": True, "fullTime": True},
            "CONTRACT",
        ),
        (
            {"temporary": True, "fullTime": True},
            "TEMPORARY",
        ),
        ({}, None),
    ],
)
def test_hiring_flags_to_employment_type(
    flags: dict[str, bool],
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCC sets multiple booleans simultaneously (e.g. ``fullTime``
    *and* ``permanent``). The mapping picks the most specific
    *commitment*-level flag first."""
    page = _wrap_page(
        [
            _job_entry(
                job_id="ht-" + (expected or "none"),
                full_time=flags.get("fullTime", False),
                part_time=flags.get("partTime", False),
                contract=flags.get("contract", False),
                permanent=flags.get("permanent", False),
                temporary=flags.get("temporary", False),
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.employment_type == expected


def test_publish_date_only_no_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCC occasionally trims publish dates to ``YYYY-MM-DD``. The
    parser accepts both forms and falls back to midnight UTC."""
    page = _wrap_page(
        [_job_entry(publish_date="2024-03-15")],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.posted_at == datetime(2024, 3, 15, 0, 0, 0, tzinfo=UTC)


def test_invalid_publish_date_falls_back_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage date (e.g. corrupted apollo state) mustn't crash the
    parse — leave ``posted_at`` ``None`` and continue."""
    page = _wrap_page([_job_entry(publish_date="not-a-date")])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert job.posted_at is None


def test_friendly_id_recovered_from_url_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older OCC builds shipped postings without ``friendlyId`` —
    parse it from the ``url`` path so canonical URLs still resolve."""
    entry = _job_entry(
        friendly_id=None,
        url_path="/empleo/oferta/55555-old-school-posting?rank=2",
        job_id="55555",
    )
    # Force friendlyId off
    entry["friendlyId"] = None
    page = _wrap_page([entry])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = OCCMexicoScraper("all", max_pages=2).fetch()
    assert str(job.url) == (
        "https://www.occ.com.mx/empleo/oferta/55555-old-school-posting/"
    )


def test_skips_entries_missing_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row without id or title is a stub posting (expired /
    confidential drafts). Skip rather than fabricate values."""
    valid = _job_entry(job_id="1")
    missing_title = _job_entry(job_id="2", title="")
    missing_id = _job_entry(job_id="3")
    missing_id["id"] = ""
    page = _wrap_page([valid, missing_title, missing_id])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=2).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_apollo_walked_directly_when_root_query_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older OCC builds / unusual URLs may ship the apollo state
    without a ``ROOT_QUERY → jobsByUrl`` entry. The parser must
    fall back to walking ``initialApolloState`` directly."""
    page = _wrap_page(
        [
            _job_entry(job_id="100", title="Job A"),
            _job_entry(job_id="200", title="Job B"),
        ],
        include_root_query=False,
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=2).fetch()
    assert sorted(j.ats_id for j in jobs) == ["100", "200"]


def test_missing_next_data_yields_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page without ``__NEXT_DATA__`` (CF interstitial slipped past
    the title check, or OCC ships an A/B variant we don't speak)
    must not crash. The pagination loop ends gracefully."""
    no_data = (
        "<!DOCTYPE html><html><head><title>OCC</title></head>"
        "<body><p>no script here</p></body></html>"
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=no_data),
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=3).fetch()
    assert jobs == []


def test_malformed_next_data_yields_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken JSON inside ``__NEXT_DATA__`` (mid-rollout, corrupted
    response) — same outcome: no rows, no crash."""
    page = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        "{not valid json"
        "</script></body></html>"
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=3).fetch()
    assert jobs == []


# --- Pagination & dedup -----------------------------------------------


def test_paginates_until_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scraper walks pages 1..N until the response yields zero
    Apollo entries, then stops. The URL ``page=`` parameter
    increments on each call."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200,
                text=_wrap_page([_job_entry(job_id="1")]),
            ),
            _StubResponse(
                status_code=200,
                text=_wrap_page([_job_entry(job_id="2")]),
            ),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]
    urls = [g["url"] for g in session.gets]
    assert urls[0] == "https://www.occ.com.mx/empleos"
    assert "page=2" in urls[1]
    assert "page=3" in urls[2]


def test_dedupes_sticky_jobs_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCC re-renders premium / sticky postings on every page. The
    scraper must dedupe by ``Job:<id>`` and stop when a page yields
    zero *new* rows — otherwise it'd loop forever on a sticky
    tail."""
    job_a = _job_entry(job_id="1")
    job_b = _job_entry(job_id="2")
    job_c = _job_entry(job_id="3")
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=_wrap_page([job_a, job_b])),
            # Page 2 reshows job_a (sticky) plus a new job_c.
            _StubResponse(status_code=200, text=_wrap_page([job_a, job_c])),
            # Page 3 is all old — should trigger the "no new in page"
            # short-circuit.
            _StubResponse(status_code=200, text=_wrap_page([job_a, job_b])),
            # If pagination didn't stop above, this would blow up.
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=10).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "3"]


def test_respects_max_pages_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if OCC keeps serving fresh rows, the walk stops after
    ``max_pages``. Prevents a runaway loop if OCC regresses on the
    empty-page sentinel."""
    pages = [
        _StubResponse(
            status_code=200,
            text=_wrap_page([_job_entry(job_id=str(i))]),
        )
        for i in range(1, 6)
    ]
    session = _install_session(monkeypatch, pages)
    jobs = OCCMexicoScraper("all", max_pages=3).fetch()
    assert len(jobs) == 3
    # Only three GETs should have happened — anything more would
    # mean ``max_pages`` was ignored.
    assert len(session.gets) == 3


# --- Cloudflare preset rotation ---------------------------------------


def test_first_preset_blocked_falls_through_to_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloudflare's matrix is non-deterministic. When the first
    preset returns the JS challenge body, the scraper closes that
    session and tries the next preset. The cleared session takes
    over for the rest of the walk."""
    sessions = _install_session_factory(
        monkeypatch,
        [
            # preset 1 — blocked
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)],
            # preset 2 — cleared, returns the page + empty page-2
            [
                _StubResponse(
                    status_code=200,
                    text=_wrap_page([_job_entry(job_id="42")]),
                ),
                _StubResponse(status_code=200, text=_wrap_page([])),
            ],
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["42"]
    # First session must have been closed before the second was used.
    assert sessions[0].closed is True
    assert sessions[1].closed is True


def test_cloudflare_200_with_challenge_body_treated_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloudflare occasionally returns a 200 status with a JS
    challenge body. Status alone isn't enough — we must also sniff
    the ``Just a moment...`` title to distinguish a real page from
    a clearing interstitial."""
    presets = len(occ_mod._HTTPCLOAK_PRESETS)
    _install_session_factory(
        monkeypatch,
        [
            [_StubResponse(status_code=200, text=_CLOUDFLARE_PAGE)]
            for _ in range(presets)
        ],
    )
    jobs = OCCMexicoScraper("all").fetch()
    assert jobs == []


# --- Mid-pagination retry ---------------------------------------------


def test_transient_429_retried_then_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single 429 on page 2 must not nuke the whole walk — backoff
    and retry the same page."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200,
                text=_wrap_page([_job_entry(job_id="1")]),
            ),
            # page 2 attempt 1 — transient 429
            _StubResponse(status_code=429, text=""),
            # page 2 attempt 2 — works
            _StubResponse(
                status_code=200,
                text=_wrap_page([_job_entry(job_id="2")]),
            ),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]
    # 4 GETs: page 1, page 2 (x2), page 3.
    assert len(session.gets) == 4


def test_persistent_5xx_breaks_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three failed retries on a mid-walk page ends the loop but
    keeps the rows we already have — partial data is preferable to
    crashing the publish pipeline."""
    _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200,
                text=_wrap_page([_job_entry(job_id="1")]),
            ),
            _StubResponse(status_code=503, text=""),
            _StubResponse(status_code=503, text=""),
            _StubResponse(status_code=503, text=""),
        ],
    )
    jobs = OCCMexicoScraper("all", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_session_closed_after_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The httpcloak Session must be closed when the scraper
    finishes — leaking a session leaks the underlying TLS pool.
    The ``_SuppressedClose`` wrapper guarantees no double-fault."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200,
                text=_wrap_page([_job_entry(job_id="1")]),
            ),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    OCCMexicoScraper("all", max_pages=2).fetch()
    assert session.closed is True
