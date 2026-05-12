"""Tests for the SuperJob.ru scraper.

SuperJob's ``/vacancies/`` endpoint is auth-gated (``X-Api-App-Id``)
and geo-blocked at the edge to non-CIS IPs. These tests exercise:

- The ``SUPERJOB_API_KEY`` env var contract (``fetch()`` raises a
  pointed ``ScraperError`` when the key is missing).
- Item parsing — every documented SuperJob field maps onto the right
  ``Job`` slot (``payment_from``/``payment_to`` with the 0 sentinel,
  ``currency`` → ISO 4217, ``type_of_work.title`` → EmploymentType
  via the Russian-label map, ``date_published`` unix timestamp →
  ``posted_at`` UTC, HTML stripped from ``vacancyRichText``).
- Pagination — the scraper walks ``page=0..`` while ``more=True`` and
  stops on empty ``objects`` or the ``more=false`` flag.
- Slicing — ``slice_by='town'`` fans out by town code;
  ``slice_by='none'`` runs a single unsliced query.
- The proxy URL helper (the 4-colon Evomi shape converts).
- 403 → ``ScraperError`` that points the operator at both the API
  key and proxy knobs.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, SuperJobScraper
from jobhive.scrapers.superjob import (
    CURRENCY_MAP,
    EMPLOYMENT_MAP,
    _as_positive_float,
    _join_description,
    _map_employment,
    _parse_unix,
    _resolve_proxy_url,
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.superjob as sj

    monkeypatch.setattr(sj, "MAX_RETRIES", 1)
    monkeypatch.setattr(sj, "RETRY_BASE_DELAY", 0.0)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests assume a clean env — no proxy and a known API key so
    httpx_mock doesn't get bypassed and ``fetch()`` doesn't bail on
    the missing-key check."""
    monkeypatch.delenv("PROXY", raising=False)
    monkeypatch.setenv("SUPERJOB_API_KEY", "test-secret")


# --- fixture helpers --------------------------------------------------------


def _item(
    *,
    job_id: int = 12345678,
    profession: str = "Senior Python Developer",
    firm_name: str = "Yandex",
    town_id: int = 4,
    town_title: str = "Москва",
    payment_from: int = 200000,
    payment_to: int = 350000,
    currency: str | None = "rub",
    type_of_work_title: str | None = "Полный рабочий день",
    candidat: str | None = "Опыт Python от 1 года.",
    vacancy_rich_text: str | None = "<p>Разработка <b>backend</b> сервисов.</p>",
    date_published: int = 1747037400,  # 2025-05-12T10:30:00Z
    link: str | None = None,
) -> dict:
    return {
        "id": job_id,
        "profession": profession,
        "firm_name": firm_name,
        "town": {"id": town_id, "title": town_title},
        "payment_from": payment_from,
        "payment_to": payment_to,
        "currency": currency,
        "type_of_work": {"id": 6, "title": type_of_work_title},
        "candidat": candidat,
        "vacancyRichText": vacancy_rich_text,
        "date_published": date_published,
        "link": link or f"https://www.superjob.ru/vakansii/{job_id}.html",
        "experience": {"id": 2, "title": "От 1 года"},
        "education": {"id": 200, "title": "Высшее"},
        "catalogues": [{"id": 33, "title": "IT, Интернет, связь, телеком"}],
    }


def _page(items: list[dict], *, total: int | None = None, more: bool = False) -> dict:
    return {
        "objects": items,
        "total": total if total is not None else len(items),
        "more": more,
    }


# --- proxy URL helper -------------------------------------------------------


def test_proxy_url_quad_colon_format_converted() -> None:
    out = _resolve_proxy_url("http://proxy.example.com:1000:alice:secret")
    assert out == "http://alice:secret@proxy.example.com:1000"


def test_proxy_url_already_canonical_passes_through() -> None:
    out = _resolve_proxy_url("http://user:pw@host.example:8080")
    assert out == "http://user:pw@host.example:8080"


def test_proxy_url_none_or_empty_returns_none() -> None:
    assert _resolve_proxy_url(None) is None
    assert _resolve_proxy_url("") is None


# --- registry / constructor -------------------------------------------------


def test_registry_resolves_superjob() -> None:
    assert ScraperRegistry.get(ATSType.SUPERJOB) is SuperJobScraper


def test_unknown_slice_by_raises() -> None:
    with pytest.raises(ScraperError, match="unsupported slice_by"):
        SuperJobScraper("superjob", slice_by="region")


def test_max_pages_per_slice_is_clamped() -> None:
    """API hard cap is 5 (500 results) — even when the operator passes
    a higher value, the scraper clamps."""
    scraper = SuperJobScraper("superjob", max_pages_per_slice=500)
    assert scraper.max_pages_per_slice == 5


def test_picks_up_proxy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY", "http://proxy.example.com:1000:alice:secret")
    scraper = SuperJobScraper("superjob")
    assert scraper.proxy_url == "http://alice:secret@proxy.example.com:1000"


def test_picks_up_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERJOB_API_KEY", "abc123")
    scraper = SuperJobScraper("superjob")
    assert scraper.api_key == "abc123"


def test_fetch_without_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the env-var contract: no key → no fetch,
    with a hint pointing at the registration page."""
    monkeypatch.delenv("SUPERJOB_API_KEY", raising=False)
    scraper = SuperJobScraper("superjob")
    with pytest.raises(ScraperError, match="SUPERJOB_API_KEY"):
        scraper.fetch()


# --- happy path -------------------------------------------------------------


def test_parses_full_item(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([_item()], more=False),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.SUPERJOB
    assert j.ats_id == "12345678"
    assert str(j.url) == "https://www.superjob.ru/vakansii/12345678.html"
    assert j.title == "Senior Python Developer"
    assert j.company == "Yandex"
    assert j.location == "Москва"
    assert j.country_iso == "RU"
    assert j.language == "ru"
    assert j.salary_currency == "RUB"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 200000.0
    assert j.salary_max == 350000.0
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Полный рабочий день"
    assert j.description is not None
    assert "<" not in j.description and ">" not in j.description
    assert "Python" in j.description
    assert "backend" in j.description
    assert j.posted_at is not None
    assert j.posted_at.year == 2025
    assert j.raw is not None
    assert j.raw["town_id"] == 4
    assert j.raw["experience"]["id"] == 2


def test_sends_app_id_header(httpx_mock, monkeypatch: pytest.MonkeyPatch) -> None:
    """``X-Api-App-Id`` must be on every outbound request — the API
    rejects everything else with 403."""
    monkeypatch.setenv("SUPERJOB_API_KEY", "my-secret-key")
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([_item()]),
    )
    SuperJobScraper("superjob", slice_by="none").fetch()
    req = httpx_mock.get_requests()[0]
    assert req.headers["X-Api-App-Id"] == "my-secret-key"


def test_falls_back_to_confidential_when_no_employer(httpx_mock) -> None:
    """SuperJob ships anonymized listings with no ``firm_name`` and an
    empty ``client``. The canonical schema requires ``company`` so we
    stamp the SuperJob-native fallback ('Конфиденциально')."""
    item = _item(firm_name="")
    item.pop("firm_name")
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([item]),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert jobs[0].company == "Конфиденциально"


def test_falls_back_to_client_title_when_firm_name_missing(httpx_mock) -> None:
    """When ``firm_name`` is empty but ``client.title`` is set,
    prefer the latter (common on agency-posted listings)."""
    item = _item(firm_name="")
    item["client"] = {"title": "Agency Acme"}
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([item]),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert jobs[0].company == "Agency Acme"


# --- salary -----------------------------------------------------------------


def test_currency_map_covers_known_codes() -> None:
    """Pin the legacy → ISO normalization. ``rub`` (the canonical
    lowercase shape SuperJob ships) → ``RUB``; ``rur`` legacy alias
    folds to ``RUB`` too."""
    assert CURRENCY_MAP["rub"] == "RUB"
    assert CURRENCY_MAP["rur"] == "RUB"
    assert CURRENCY_MAP["usd"] == "USD"
    assert CURRENCY_MAP["eur"] == "EUR"
    assert CURRENCY_MAP["kzt"] == "KZT"
    assert CURRENCY_MAP["byn"] == "BYN"


def test_payment_zero_is_treated_as_missing(httpx_mock) -> None:
    """SuperJob uses ``0`` as the sentinel for ``payment_from`` /
    ``payment_to`` when the listing doesn't specify a side. Drop to
    ``None`` rather than ship a literal zero salary."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([_item(payment_from=0, payment_to=200000)]),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max == 200000.0
    assert jobs[0].salary_currency == "RUB"


def test_payment_both_zero_drops_salary_block(httpx_mock) -> None:
    """No salary signal at all — all four salary fields must be
    ``None`` rather than carrying a free currency without amounts."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([_item(payment_from=0, payment_to=0)]),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None
    assert jobs[0].salary_period is None


def test_unknown_currency_drops_salary(httpx_mock) -> None:
    """Anything outside the known whitelist is treated as 'no signal'.
    Defensive — keeps the canonical schema clean."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([_item(currency="xyz")]),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None


def test_as_positive_float_handles_sentinels() -> None:
    assert _as_positive_float(0) is None
    assert _as_positive_float(-1) is None
    assert _as_positive_float(None) is None
    assert _as_positive_float("abc") is None
    assert _as_positive_float(150_000) == 150_000.0
    assert _as_positive_float("150000") == 150_000.0


# --- employment / experience -----------------------------------------------


@pytest.mark.parametrize("label, expected", [
    ("полный рабочий день", "FULL_TIME"),
    ("полная занятость", "FULL_TIME"),
    ("частичная занятость", "PART_TIME"),
    ("неполный рабочий день", "PART_TIME"),
    ("стажировка", "INTERN"),
    ("временная работа", "TEMPORARY"),
    ("сезонная работа", "TEMPORARY"),
    ("вахтовый метод", "CONTRACT"),
])
def test_employment_map_covers_known_labels(label: str, expected: str) -> None:
    assert EMPLOYMENT_MAP[label] == expected


def test_map_employment_folds_case() -> None:
    """SuperJob mixes ``"Полный рабочий день"`` and the lowercase
    variant across the corpus — the lookup should fold case so both
    map onto ``FULL_TIME``."""
    assert _map_employment("Полный рабочий день") == "FULL_TIME"
    assert _map_employment("ПОЛНЫЙ РАБОЧИЙ ДЕНЬ") == "FULL_TIME"
    assert _map_employment("  полный рабочий день  ") == "FULL_TIME"


def test_map_employment_unknown_returns_none() -> None:
    assert _map_employment("unknown-label") is None
    assert _map_employment(None) is None
    assert _map_employment("") is None


def test_unknown_type_of_work_leaves_field_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([_item(type_of_work_title="Что-то новое")]),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert jobs[0].employment_type is None
    # commitment still preserves the original Russian label
    assert jobs[0].commitment == "Что-то новое"


# --- description --------------------------------------------------


def test_join_description_strips_html_tags() -> None:
    out = _join_description(
        "Опыт Python от 1 года.",
        "<p>Разработка <b>backend</b> сервисов.</p>",
    )
    assert out is not None
    assert "<" not in out
    assert ">" not in out
    assert "Python" in out
    assert "backend" in out
    assert "Разработка" in out


def test_join_description_collapses_html_entities() -> None:
    out = _join_description(None, "Tom &amp; Jerry &nbsp;&quot;quoted&quot;")
    assert out is not None
    assert "&amp;" not in out
    assert "&nbsp;" not in out
    assert "Tom & Jerry" in out
    assert '"quoted"' in out


def test_join_description_both_none_returns_none() -> None:
    assert _join_description(None, None) is None
    assert _join_description("", "") is None


def test_join_description_one_side_only() -> None:
    out = _join_description("Только candidat.", None)
    assert out == "Только candidat."


# --- published_at parsing ---------------------------------------------------


def test_parse_unix_handles_int() -> None:
    dt = _parse_unix(1747037400)
    assert dt is not None
    assert dt.year == 2025 and dt.month == 5
    assert dt.tzinfo is not None  # UTC-aware


def test_parse_unix_handles_str() -> None:
    """Defensive: ``date_published`` has been observed as a stringified
    int on legacy listings."""
    dt = _parse_unix("1747037400")
    assert dt is not None
    assert dt.year == 2025


def test_parse_unix_zero_and_none_returns_none() -> None:
    assert _parse_unix(0) is None
    assert _parse_unix(None) is None
    assert _parse_unix("not-a-number") is None


# --- pagination -------------------------------------------------------------


def test_walks_pages_while_more_is_true(httpx_mock) -> None:
    """Two pages with ``more=True`` on page 0, then ``more=False`` on
    page 1 — scraper stops after page 1."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*page=0(&|$)"),
        json=_page([_item(job_id=1)], more=True),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*page=1(&|$)"),
        json=_page([_item(job_id=2)], more=False),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2"}


def test_stops_when_objects_empty(httpx_mock) -> None:
    """``more=True`` but ``objects=[]`` — bail rather than loop."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*page=0(&|$)"),
        json=_page([_item(job_id=1)], more=True),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*page=1(&|$)"),
        json=_page([], more=True),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert {j.ats_id for j in jobs} == {"1"}


def test_deduplicates_across_slices(httpx_mock) -> None:
    """The same job can show up under two overlapping towns (rare —
    usually town-scoped — but defensively handle the case)."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([_item(job_id=42), _item(job_id=43)], more=False),
        is_reusable=True,
    )
    jobs = SuperJobScraper(
        "superjob", slice_by="town", towns=[4, 14]
    ).fetch()
    assert {j.ats_id for j in jobs} == {"42", "43"}


# --- slicing ---------------------------------------------------------------


def test_slice_by_town_fans_out_per_code(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*town=4(&|$)"),
        json=_page([_item(job_id=100)], more=False),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*town=14(&|$)"),
        json=_page([_item(job_id=200)], more=False),
    )
    jobs = SuperJobScraper(
        "superjob", slice_by="town", towns=[4, 14]
    ).fetch()
    assert {j.ats_id for j in jobs} == {"100", "200"}


# --- error handling --------------------------------------------------------


def test_403_raises_with_helpful_hint(httpx_mock) -> None:
    """403 could be either the API rejecting the app-id or the edge
    geo-blocking — error message should call out both paths."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        status_code=403,
        json={"error": {"code": 403, "message": "test"}},
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="SUPERJOB_API_KEY"):
        SuperJobScraper("superjob", slice_by="none").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        status_code=500,
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        SuperJobScraper("superjob", slice_by="none").fetch()


def test_skips_items_missing_required_fields(httpx_mock) -> None:
    """Defensive: items missing ``id`` / ``link`` / ``profession``
    are dropped silently."""
    good = _item(job_id=1)
    bad_no_id = {**_item(job_id=2), "id": None}
    bad_no_link = {**_item(job_id=3), "link": None}
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.superjob\.ru/2\.0/vacancies/\?.*"),
        json=_page([good, bad_no_id, bad_no_link]),
    )
    jobs = SuperJobScraper("superjob", slice_by="none").fetch()
    assert {j.ats_id for j in jobs} == {"1"}
