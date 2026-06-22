"""Tests for the InfoJobs Spain scraper.

InfoJobs Spain is a React SPA whose listing pages embed the entire
search payload in ``window.__INITIAL_PROPS__ = JSON.parse("…");``.
The scraper extracts that hydration blob and parses ``offers[]``
directly — no DOM walking required. The site is gated by Distil +
Geetest, so the only transport is ``httpcloak``.

These tests exercise:

- The registry resolves the ``infojobs_es`` ATS type to the scraper.
- The hydration extractor handles the JSON-encoded-as-JSON shape.
- Card → Job mapping for every field the scraper populates.
- Spanish contract-type label → EmploymentType normalization.
- Teleworking label → ``is_remote`` inference (only when stated).
- Salary payload parsing (range / period / currency).
- Pagination stops on totalElements, three empty pages, or max_pages.
- ``httpcloak`` is monkey-patched — we never hit the live site.
"""

from __future__ import annotations

import json
import os
from datetime import UTC
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import InfoJobsSpainScraper, ScraperRegistry
from jobhive.scrapers.infojobs_es import (
    _absolutize_link,
    _extract_initial_props,
    _fmt_amount,
    _infer_remote,
    _page_url,
    _parse_published_at,
    _parse_salary,
)

# --- live e2e ---------------------------------------------------------------


def test_live_e2e_fetches_real_infojobs_page() -> None:
    """Opt-in smoke test against the real InfoJobs Spain listing page.

    Normal CI keeps this skipped because it depends on the public site and
    httpcloak. Run with ``JOBHIVE_LIVE_E2E=1`` when reviewing the scraper PR.
    """
    if os.environ.get("JOBHIVE_LIVE_E2E") != "1":
        pytest.skip("set JOBHIVE_LIVE_E2E=1 to hit the real infojobs.net site")

    from importlib.util import find_spec

    if find_spec("httpcloak") is None:
        pytest.skip("httpcloak is required for live InfoJobs Spain e2e")

    import jobhive.scrapers.infojobs_es as ij

    ij.MAX_RETRIES = 3
    ij.RETRY_BASE_DELAY = 1.5

    jobs = InfoJobsSpainScraper("infojobs_es", max_pages=1, timeout=30).fetch()

    assert jobs
    for job in jobs[:5]:
        assert job.ats_type is ATSType.INFOJOBSES
        assert job.ats_id
        assert job.title
        assert job.company
        assert str(job.url).startswith("https://www.infojobs.net/")
        print(job.title, job.company, job.location, job.url, sep=" | ")


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse retry backoff so the timing-sensitive tests stay quick."""
    import jobhive.scrapers.infojobs_es as ij
    monkeypatch.setattr(ij, "MAX_RETRIES", 1)
    monkeypatch.setattr(ij, "RETRY_BASE_DELAY", 0.0)


def _offer(
    *,
    code: str = "abc123def4567890abcdef0123456789",
    title: str = "Ingeniero de Software",
    description: str = "Buscamos un ingeniero senior.",
    city: str | None = "Madrid",
    link: str | None = None,
    contract_type: str | None = "Contrato indefinido",
    workday: str | None = "Jornada completa",
    teleworking: str | None = "Presencial",
    published_at: str = "2026-05-12T10:41:35Z",
    company_name: str = "Acme S.A.",
    company_link: str | None = "https://acme.ofertas-trabajo.infojobs.net",
    salary: dict[str, Any] | None = None,
    executive: bool = False,
    states: list[str] | None = None,
) -> dict[str, Any]:
    """Build an ``offer`` dict matching the live ``__INITIAL_PROPS__``
    schema. Extra keys are ignored by the scraper so we don't bother
    populating them in tests."""
    if link is None:
        link = f"//www.infojobs.net/madrid/ingeniero-software/of-i{code}"
    obj: dict[str, Any] = {
        "code": code,
        "title": title,
        "description": description,
        "city": city,
        "link": link,
        "contractType": contract_type,
        "workday": workday,
        "teleworking": teleworking,
        "publishedAt": published_at,
        "companyName": company_name,
        "companyLink": company_link,
        "states": states or [],
        "executive": executive,
    }
    if salary is not None:
        obj["salary"] = salary
    return obj


def _hydration_html(
    offers: list[dict[str, Any]], *, total_elements: int | None = None,
) -> str:
    """Wrap a list of offers in the double-encoded ``__INITIAL_PROPS__``
    shape the live site ships. We control the encoding here so the
    tests pin the exact JSON-as-string-as-JSON contract the parser
    relies on."""
    payload: dict[str, Any] = {"offers": offers, "search": {}}
    if total_elements is not None:
        payload["overview"] = {"totalElements": total_elements}
    inner = json.dumps(payload, ensure_ascii=False)
    quoted = json.dumps(inner, ensure_ascii=False)  # JS string literal
    return (
        '<html><head></head><body><script>'
        f'window.__INITIAL_PROPS__ = JSON.parse({quoted});'
        'window.__APP_CONFIG__ = {};'
        '</script></body></html>'
    )


class _FakeHttpcloakResponse:
    def __init__(self, *, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.content = text.encode("utf-8")


@pytest.fixture
def fake_httpcloak(monkeypatch: pytest.MonkeyPatch):
    """Patch ``httpcloak.get`` and ``importlib.find_spec`` so the
    scraper's transport layer goes through a per-test queue. The
    queue is exposed as ``fake.responses`` — push (page, html) pairs
    in the order pages will be requested."""

    class Fake:
        def __init__(self) -> None:
            self.responses: list[tuple[int, str] | tuple[int, str, int]] = []
            self.calls: list[str] = []

        def queue(
            self, *, html: str = "", status: int = 200,
        ) -> None:
            self.responses.append((status, html))

        def queue_status(self, status: int) -> None:
            self.responses.append((status, ""))

    fake = Fake()

    def fake_get(url: str, timeout: float = 30.0):
        del timeout  # ignored — the fake response is canned
        fake.calls.append(url)
        if not fake.responses:
            return _FakeHttpcloakResponse(
                status_code=200,
                text=_hydration_html([], total_elements=0),
            )
        status, text, *_ = fake.responses.pop(0)
        return _FakeHttpcloakResponse(status_code=status, text=text)

    # Inject a stub ``httpcloak`` module so ``find_spec`` sees it and
    # the local import inside the scraper resolves to our fake. The
    # ``__spec__`` attribute has to be a real ModuleSpec (not None)
    # because ``importlib.util.find_spec`` raises ``ValueError`` when
    # a module is registered in ``sys.modules`` without one.
    import sys
    import types
    from importlib.machinery import ModuleSpec
    stub = types.ModuleType("httpcloak")
    stub.__spec__ = ModuleSpec("httpcloak", loader=None)
    stub.get = fake_get  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpcloak", stub)

    return fake


# --- registry & enum --------------------------------------------------------


def test_registry_resolves_infojobs_es() -> None:
    assert ScraperRegistry.get(ATSType.INFOJOBSES) is InfoJobsSpainScraper


def test_ats_type_value_is_infojobs_es() -> None:
    """The enum value is the storage path + CSV column suffix — pin it."""
    assert ATSType.INFOJOBSES.value == "infojobs_es"


# --- hydration extraction ---------------------------------------------------


def test_extract_initial_props_decodes_double_encoded_payload() -> None:
    """The live site wraps the JSON document in a JS string literal
    that's itself a JSON string. Verify the parser walks both layers."""
    html_text = _hydration_html([_offer()], total_elements=42)
    data = _extract_initial_props(html_text)
    assert isinstance(data, dict)
    assert data["overview"]["totalElements"] == 42
    assert len(data["offers"]) == 1
    assert data["offers"][0]["title"] == "Ingeniero de Software"


def test_extract_initial_props_raises_when_marker_missing() -> None:
    """Distil captcha pages don't contain the marker at all — raising
    surfaces the failure rather than silently returning zero jobs."""
    with pytest.raises(ScraperError, match="__INITIAL_PROPS__ not found"):
        _extract_initial_props("<html>captcha</html>")


def test_extract_initial_props_handles_escaped_quotes() -> None:
    """The description field commonly contains quotes (``\"Senior\"``)
    which double-encode through both JSON.parse layers. If the walker
    miscounts escapes, the payload terminates mid-string and parsing
    breaks. This pins the contract."""
    offers = [_offer(
        description='Texto con "comillas" y backslash \\ aquí.',
    )]
    html_text = _hydration_html(offers)
    data = _extract_initial_props(html_text)
    assert data["offers"][0]["description"].endswith("aquí.")


def test_extract_initial_props_allows_spaced_json_parse_argument() -> None:
    inner = json.dumps({"offers": [_offer()], "search": {}}, ensure_ascii=False)
    quoted = json.dumps(inner, ensure_ascii=False)
    html_text = (
        "<script>window.__INITIAL_PROPS__ = "
        f"JSON.parse(  {quoted}  );</script>"
    )

    data = _extract_initial_props(html_text)

    assert data["offers"][0]["title"] == "Ingeniero de Software"


def test_extract_initial_props_allows_single_quoted_json_parse_argument() -> None:
    inner = json.dumps({"offers": [_offer()], "search": {}}, ensure_ascii=False)
    quoted = "'" + inner.replace("\\", "\\\\").replace("'", "\\'") + "'"
    html_text = f"<script>window.__INITIAL_PROPS__ = JSON.parse({quoted});</script>"

    data = _extract_initial_props(html_text)

    assert data["offers"][0]["title"] == "Ingeniero de Software"


# --- happy path --------------------------------------------------------------


def test_parses_full_offer(fake_httpcloak) -> None:
    """Every field on a fully-populated offer maps to the right Job
    column. Pin the contract so a renamed/missed field surfaces here."""
    fake_httpcloak.queue(html=_hydration_html([_offer(
        code="2f89672b774743a64e1a69c040f988",
        title="Limpiador/a - Cala Millor",
        description="Únete a nuestro equipo.",
        city="Son Servera",
        link="//www.infojobs.net/son-servera/limpiador/of-i2f89672b774743a64e1a69c040f988?page=1",
        contract_type="Contrato indefinido",
        workday="Jornada completa",
        teleworking="Presencial",
        published_at="2026-05-12T10:41:35Z",
        company_name="Hipotels",
        salary={
            "range": {"min": 1200, "max": 1500},
            "period": "MONTH",
            "currency": "EUR",
            "type": "GROSS",
        },
    )], total_elements=1))

    jobs = InfoJobsSpainScraper("any", max_pages=1).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.INFOJOBSES
    assert j.ats_id == "2f89672b774743a64e1a69c040f988"
    assert j.title == "Limpiador/a - Cala Millor"
    assert j.company == "Hipotels"
    assert j.location == "Son Servera"
    assert j.country_iso == "ES"
    assert j.language == "es"
    assert j.is_remote is False
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Contrato indefinido"
    assert j.salary_currency == "EUR"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 1200.0
    assert j.salary_max == 1500.0
    assert "1.200" in (j.salary_summary or "")
    assert "1.500" in (j.salary_summary or "")
    assert j.posted_at is not None
    assert j.posted_at.year == 2026 and j.posted_at.month == 5
    assert j.posted_at.tzinfo is UTC
    assert j.fetched_at.tzinfo is not None
    assert str(j.url).startswith("https://www.infojobs.net/son-servera/")
    assert j.raw is not None
    assert j.raw["workday"] == "Jornada completa"
    assert j.raw["modality"] == "Presencial"
    assert j.raw["contract_type"] == "Contrato indefinido"


def test_skips_offer_without_required_fields(fake_httpcloak) -> None:
    """Missing ``code``/``title``/``link`` should drop the row rather
    than ship a Job with bogus values. ``total_elements=0`` short-
    circuits pagination after the first empty page."""
    fake_httpcloak.queue(html=_hydration_html([
        {"title": "no code", "link": "/x"},
        _offer(code="real001", title="Real Job"),
    ], total_elements=1))
    jobs = InfoJobsSpainScraper("any", max_pages=1).fetch()
    assert [j.ats_id for j in jobs] == ["real001"]


# --- contract-type mapping ---------------------------------------------------


@pytest.mark.parametrize("label, expected", [
    ("Contrato indefinido", "FULL_TIME"),
    ("Indefinido", "FULL_TIME"),
    ("Contrato de duración determinada", "TEMPORARY"),
    ("Duración determinada", "TEMPORARY"),
    ("Contrato fijo discontinuo", "TEMPORARY"),
    ("Prácticas", "INTERN"),
    ("Becario", "INTERN"),
    ("Contrato de prácticas", "INTERN"),
    ("Otros contratos", "CONTRACT"),
    ("Autónomo", "CONTRACT"),
])
def test_contract_type_maps_to_employment_type(
    fake_httpcloak, label: str, expected: str,
) -> None:
    fake_httpcloak.queue(html=_hydration_html([
        _offer(contract_type=label),
    ], total_elements=1))
    jobs = InfoJobsSpainScraper("any", max_pages=1).fetch()
    assert jobs[0].employment_type == expected
    assert jobs[0].commitment == label


def test_unknown_contract_type_leaves_employment_type_none(
    fake_httpcloak,
) -> None:
    """A label we haven't mapped surfaces as ``commitment`` only —
    we don't want to silently coerce unknowns into FULL_TIME."""
    fake_httpcloak.queue(html=_hydration_html([
        _offer(contract_type="Voluntariado lunar"),
    ], total_elements=1))
    jobs = InfoJobsSpainScraper("any", max_pages=1).fetch()
    assert jobs[0].employment_type is None
    assert jobs[0].commitment == "Voluntariado lunar"


# --- teleworking → is_remote inference --------------------------------------


@pytest.mark.parametrize("label, expected", [
    ("Presencial", False),
    ("Híbrido", False),
    ("Remoto", True),
    ("100% remoto", True),
    ("Teletrabajo", True),
    (None, None),
    ("", None),
    ("Otra cosa", None),
])
def test_infer_remote(label, expected) -> None:
    assert _infer_remote(label) is expected


# --- salary parsing ---------------------------------------------------------


def test_parse_salary_full_range() -> None:
    smin, smax, cur, per, sm = _parse_salary({
        "range": {"min": 1200, "max": 1500},
        "period": "MONTH",
        "currency": "EUR",
    })
    assert smin == 1200.0 and smax == 1500.0
    assert cur == "EUR" and per == "MONTH"
    assert sm is not None and "€" in sm and "mes" in sm


def test_parse_salary_min_only() -> None:
    smin, smax, _cur, per, sm = _parse_salary({
        "range": {"min": 30000, "max": 0},
        "period": "YEAR",
        "currency": "EUR",
    })
    assert smin == 30000.0 and smax is None
    assert per == "YEAR"
    assert sm is not None and "desde" in sm and "año" in sm


def test_parse_salary_max_only() -> None:
    smin, smax, _cur, _per, sm = _parse_salary({
        "range": {"min": None, "max": 45000},
        "period": "YEAR",
        "currency": "EUR",
    })
    assert smin is None and smax == 45000.0
    assert sm is not None and "hasta" in sm


def test_parse_salary_missing_returns_all_none() -> None:
    assert _parse_salary(None) == (None, None, None, None, None)
    assert _parse_salary({}) == (None, None, None, None, None)
    # Range present but both bounds zero → no signal.
    assert _parse_salary({
        "range": {"min": 0, "max": 0},
        "period": "MONTH",
        "currency": "EUR",
    }) == (None, None, None, None, None)


def test_parse_salary_unknown_period_is_dropped() -> None:
    """An unrecognized period shouldn't smuggle a value into the Job —
    keep ``salary_period=None`` rather than guessing."""
    smin, smax, cur, per, _sm = _parse_salary({
        "range": {"min": 100, "max": 200},
        "period": "DECADE",
        "currency": "EUR",
    })
    assert smin == 100.0 and smax == 200.0
    assert cur == "EUR"
    assert per is None


@pytest.mark.parametrize(
    ("period", "label"), [("WEEK", "/ semana"), ("DAY", "/ día")],
)
def test_parse_salary_summary_includes_week_and_day_periods(
    period: str, label: str,
) -> None:
    _smin, _smax, _cur, parsed_period, summary = _parse_salary({
        "range": {"min": 100, "max": 200},
        "period": period,
        "currency": "EUR",
    })
    assert parsed_period == period
    assert summary is not None and label in summary


def test_fmt_amount_uses_spanish_thousands_separator() -> None:
    assert _fmt_amount(1500) == "1.500"
    assert _fmt_amount(45000) == "45.000"
    assert _fmt_amount(1_000_000) == "1.000.000"


# --- published_at parsing ---------------------------------------------------


def test_parse_published_at_iso_with_z() -> None:
    dt = _parse_published_at("2026-05-12T10:41:35Z")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 5, 12)
    assert dt.tzinfo is UTC


def test_parse_published_at_normalizes_offsets_to_utc() -> None:
    dt = _parse_published_at("2026-05-12T12:41:35+02:00")
    assert dt is not None
    assert dt.tzinfo is UTC
    assert dt.hour == 10


def test_parse_published_at_invalid_returns_none() -> None:
    assert _parse_published_at(None) is None
    assert _parse_published_at("") is None
    assert _parse_published_at("not-a-date") is None


# --- link absolutization ----------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("//www.infojobs.net/path/of-iX", "https://www.infojobs.net/path/of-iX"),
    ("/path/of-iX", "https://www.infojobs.net/path/of-iX"),
    ("https://www.infojobs.net/x", "https://www.infojobs.net/x"),
    ("http://www.infojobs.net/x", "http://www.infojobs.net/x"),
])
def test_absolutize_link(raw: str, expected: str) -> None:
    assert _absolutize_link(raw) == expected


# --- pagination -------------------------------------------------------------


def test_paginates_until_total_elements(fake_httpcloak) -> None:
    """``totalElements`` from page 1 caps the loop early — we don't
    keep walking past the catalogue. ``max_pages`` is a safety net."""
    fake_httpcloak.queue(html=_hydration_html(
        [_offer(code=f"p1-{i}") for i in range(3)],
        total_elements=5,
    ))
    fake_httpcloak.queue(html=_hydration_html(
        [_offer(code=f"p2-{i}") for i in range(2)],
        total_elements=5,
    ))
    fake_httpcloak.queue(html=_hydration_html([], total_elements=5))
    jobs = InfoJobsSpainScraper("any", max_pages=10).fetch()
    assert len(jobs) == 5
    # Stopped after page 2 (5 jobs collected) — page 3 not fetched.
    assert len(fake_httpcloak.calls) == 2


def test_pagination_stops_on_three_consecutive_empty_pages(
    fake_httpcloak,
) -> None:
    """When ``totalElements`` is missing the loop has to fall back on
    a quiet-tail heuristic. Three empty pages in a row stops cleanly."""
    fake_httpcloak.queue(html=_hydration_html([_offer(code="x1")]))
    for _ in range(4):
        fake_httpcloak.queue(html=_hydration_html([]))
    jobs = InfoJobsSpainScraper("any", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["x1"]
    # 1 page with data + 3 empty pages before stopping.
    assert len(fake_httpcloak.calls) == 4


def test_max_pages_caps_the_loop(fake_httpcloak) -> None:
    """Even when every page returns data the scraper never goes past
    ``max_pages`` — bounding worst-case cost."""
    for i in range(5):
        fake_httpcloak.queue(html=_hydration_html([_offer(code=f"x{i}")]))
    jobs = InfoJobsSpainScraper("any", max_pages=2).fetch()
    assert len(jobs) == 2
    assert len(fake_httpcloak.calls) == 2


def test_max_pages_is_lower_bounded(fake_httpcloak) -> None:
    fake_httpcloak.queue(html=_hydration_html([_offer(code="x1")], total_elements=1))

    jobs = InfoJobsSpainScraper("any", max_pages=0).fetch()

    assert [j.ats_id for j in jobs] == ["x1"]
    assert len(fake_httpcloak.calls) == 1


def test_listing_url_preserves_existing_query_params() -> None:
    assert _page_url(
        "https://www.infojobs.net/ofertas-trabajo?province=9&page=7&q=python",
        2,
    ) == "https://www.infojobs.net/ofertas-trabajo?province=9&q=python&page=2"


def test_duplicate_codes_across_pages_are_deduped(fake_httpcloak) -> None:
    """The site occasionally bumps a featured offer back onto page 2;
    de-dup on ``code`` so consumers don't get the same Job twice."""
    fake_httpcloak.queue(html=_hydration_html([_offer(code="dup")], total_elements=2))
    fake_httpcloak.queue(html=_hydration_html(
        [_offer(code="dup"), _offer(code="other")],
        total_elements=2,
    ))
    fake_httpcloak.queue(html=_hydration_html([], total_elements=2))
    jobs = InfoJobsSpainScraper("any", max_pages=5).fetch()
    assert sorted(j.ats_id for j in jobs) == ["dup", "other"]


# --- httpcloak transport behavior -------------------------------------------


def test_page_one_hard_failure_raises(fake_httpcloak) -> None:
    """If we can't get past page 1 there's nothing to keep — raise
    so the caller knows the scrape didn't happen rather than ship []."""
    fake_httpcloak.queue_status(403)
    with pytest.raises(ScraperError):
        InfoJobsSpainScraper("any", max_pages=2).fetch()


def test_later_page_failure_keeps_collected_jobs(fake_httpcloak) -> None:
    """Deep pagination hits a wall every ~hundreds of pages. Once we
    have some jobs banked, a mid-fetch failure is logged and the
    loop terminates with what we already have."""
    fake_httpcloak.queue(html=_hydration_html([
        _offer(code="early-1"), _offer(code="early-2"),
    ]))
    fake_httpcloak.queue_status(500)
    jobs = InfoJobsSpainScraper("any", max_pages=10).fetch()
    assert sorted(j.ats_id for j in jobs) == ["early-1", "early-2"]


def test_skips_gracefully_when_httpcloak_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When the ``scrapers`` extra isn't installed we don't have a way
    past Distil. The scraper logs a warning and returns ``[]`` instead
    of crashing — pipeline keeps moving."""
    import importlib.util as _util
    real_find_spec = _util.find_spec

    def fake_find_spec(name, *a, **kw):
        if name == "httpcloak":
            return None
        return real_find_spec(name, *a, **kw)

    monkeypatch.setattr(_util, "find_spec", fake_find_spec)
    # Also ensure import-by-name fails even if a previous test stubbed
    # the module — drop it from sys.modules.
    import sys
    monkeypatch.delitem(sys.modules, "httpcloak", raising=False)

    with caplog.at_level("WARNING", logger="jobhive.scrapers.infojobs_es"):
        jobs = InfoJobsSpainScraper("any", max_pages=1).fetch()
    assert jobs == []
    assert any("httpcloak" in r.getMessage() for r in caplog.records)
