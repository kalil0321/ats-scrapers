"""Tests for the HeadHunter (hh.ru + CIS) scraper.

hh.ru exposes a JSON REST API but geo-blocks non-CIS IPs (403). These
tests exercise:

- Country-property routing (``ru`` → ``api.hh.ru``, ``kz`` → ``api.hh.kz``,
  …) — the same scraper class hits a different host depending on the
  ``company_slug`` argument.
- Item parsing — every documented hh field maps onto the right ``Job``
  slot (salary RUR → RUB, employment.id → EmploymentType,
  experience.id → integer years, snippet → plain-text description,
  HTML tags stripped).
- Pagination — the scraper walks ``page=0..pages-1`` within a slice
  and stops on empty ``items`` or when ``pages`` is exhausted.
- Slicing — ``slice_by="area"`` fans out by region code;
  ``slice_by="none"`` runs a single unsliced query.
- The proxy URL helper (the 4-colon Evomi shape converts to the
  standard URL form httpx wants).
- 403 → ScraperError that points the operator at the proxy knob.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import HHRuScraper, ScraperRegistry
from jobhive.scrapers.hhru import (
    CURRENCY_MAP,
    EMPLOYMENT_MAP,
    EXPERIENCE_MAP,
    HOSTS,
    _join_snippet,
    _parse_published_at,
    _resolve_proxy_url,
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.hhru as h

    monkeypatch.setattr(h, "MAX_RETRIES", 1)
    monkeypatch.setattr(h, "RETRY_BASE_DELAY", 0.0)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests assume a clean env so the scraper doesn't try to
    bounce httpx_mock through a real proxy."""
    monkeypatch.delenv("PROXY", raising=False)


# --- fixture helpers --------------------------------------------------------


def _item(
    *,
    job_id: str = "1234567",
    name: str = "Senior Python Developer",
    employer_name: str = "Yandex",
    employer_id: str = "1740",
    area_id: str = "1",
    area_name: str = "Москва",
    salary: dict | None = None,
    employment_id: str = "full",
    employment_name: str = "Полная занятость",
    experience_id: str = "between1And3",
    experience_name: str = "От 1 года до 3 лет",
    schedule_id: str = "fullDay",
    requirement: str | None = "Опыт <highlighttext>Python</highlighttext> от 1 года.",
    responsibility: str | None = "Разработка серверных сервисов.",
    published_at: str = "2026-05-12T10:30:00+0300",
    host: str = "hh.ru",
) -> dict:
    return {
        "id": job_id,
        "name": name,
        "alternate_url": f"https://{host}/vacancy/{job_id}",
        "employer": {"id": employer_id, "name": employer_name},
        "area": {"id": area_id, "name": area_name},
        "salary": salary,
        "snippet": {"requirement": requirement, "responsibility": responsibility},
        "employment": {"id": employment_id, "name": employment_name},
        "experience": {"id": experience_id, "name": experience_name},
        "schedule": {"id": schedule_id, "name": "Полный день"},
        "type": {"id": "open", "name": "Открытая"},
        "published_at": published_at,
    }


def _page(items: list[dict], *, page: int = 0, pages: int = 1) -> dict:
    return {
        "found": len(items),
        "items": items,
        "page": page,
        "pages": pages,
        "per_page": 100,
        "clusters": None,
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


def test_registry_resolves_hh() -> None:
    assert ScraperRegistry.get(ATSType.HH) is HHRuScraper


def test_unknown_country_slug_raises() -> None:
    with pytest.raises(ScraperError, match="unknown company_slug"):
        HHRuScraper("us")


@pytest.mark.parametrize("slug, host", list(HOSTS.items()))
def test_known_country_slugs_pick_right_host(slug: str, host: str) -> None:
    scraper = HHRuScraper(slug)
    assert scraper.host == host


def test_unknown_slice_by_raises() -> None:
    with pytest.raises(ScraperError, match="unsupported slice_by"):
        HHRuScraper("ru", slice_by="zip")


def test_max_pages_per_slice_is_clamped() -> None:
    """API hard cap is 20 (2000 results) — even when the operator
    passes a higher value, the scraper clamps."""
    scraper = HHRuScraper("ru", max_pages_per_slice=500)
    assert scraper.max_pages_per_slice == 20


def test_picks_up_proxy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY", "http://proxy.example.com:1000:alice:secret")
    scraper = HHRuScraper("ru")
    assert scraper.proxy_url == "http://alice:secret@proxy.example.com:1000"


# --- happy path -------------------------------------------------------------


def test_parses_full_item_ru(httpx_mock) -> None:
    item = _item(
        salary={"from": 200000, "to": 350000, "currency": "RUR", "gross": True},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        json=_page([item], page=0, pages=1),
    )
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.HH
    assert j.ats_id == "1234567"
    assert str(j.url) == "https://hh.ru/vacancy/1234567"
    assert j.title == "Senior Python Developer"
    assert j.company == "Yandex"
    assert j.location == "Москва"
    assert j.country_iso == "RU"
    assert j.language == "ru"
    assert j.salary_currency == "RUB"  # RUR → RUB
    assert j.salary_period == "MONTH"
    assert j.salary_min == 200000.0
    assert j.salary_max == 350000.0
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Полная занятость"
    assert j.experience == 1
    assert j.description is not None
    assert "<highlighttext>" not in j.description
    assert "Python" in j.description
    assert "Разработка" in j.description
    assert j.posted_at is not None
    assert j.posted_at.year == 2026 and j.posted_at.month == 5
    assert j.raw is not None
    assert j.raw["employer_id"] == "1740"
    assert j.raw["area_id"] == "1"
    assert j.raw["schedule"]["id"] == "fullDay"


def test_routes_kz_to_kz_host(httpx_mock) -> None:
    """``company_slug='kz'`` should hit api.hh.kz, set country_iso KZ,
    language kk. The same payload shape is reused — only the host and
    metadata change."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.kz/vacancies\?.*"),
        json=_page([_item(host="hh.kz", area_id="160", area_name="Астана")]),
    )
    jobs = HHRuScraper("kz", slice_by="none").fetch()
    assert len(jobs) == 1
    assert jobs[0].country_iso == "KZ"
    assert jobs[0].language == "kk"
    assert str(jobs[0].url).startswith("https://hh.kz/")


def test_routes_ee_to_ee_host(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ee/vacancies\?.*"),
        json=_page([_item(host="hh.ee", area_name="Tallinn")]),
    )
    jobs = HHRuScraper("ee", slice_by="none").fetch()
    assert jobs[0].country_iso == "EE"
    assert jobs[0].language == "et"


# --- salary -----------------------------------------------------------------


def test_salary_currency_map_covers_known_codes() -> None:
    """Pin the legacy → ISO normalization. RUR (legacy) → RUB; BYR
    (pre-2016 redenomination) → BYN. Everything else passes through."""
    assert CURRENCY_MAP["RUR"] == "RUB"
    assert CURRENCY_MAP["RUB"] == "RUB"
    assert CURRENCY_MAP["BYR"] == "BYN"
    assert CURRENCY_MAP["BYN"] == "BYN"
    assert CURRENCY_MAP["USD"] == "USD"
    assert CURRENCY_MAP["EUR"] == "EUR"
    assert CURRENCY_MAP["KZT"] == "KZT"


def test_salary_only_max(httpx_mock) -> None:
    """hh.ru happily ships ranges with only one side filled in
    (``from=None, to=200000``); the scraper should pass both through
    as the optional ``salary_min`` / ``salary_max`` allow."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        json=_page([
            _item(salary={"from": None, "to": 200000, "currency": "RUR"}),
        ]),
    )
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max == 200000.0
    assert jobs[0].salary_currency == "RUB"


def test_salary_missing_leaves_fields_none(httpx_mock) -> None:
    """No ``salary`` block at all — keep all four salary fields
    ``None`` instead of mis-defaulting to a free currency."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        json=_page([_item(salary=None)]),
    )
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None
    assert jobs[0].salary_period is None


def test_unknown_currency_drops_salary(httpx_mock) -> None:
    """Anything outside the known whitelist (e.g. obsolete codes) is
    treated as 'no signal' — better to drop than to ship a value the
    canonical schema can't represent."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        json=_page([
            _item(salary={"from": 1000, "to": 2000, "currency": "ZZZ"}),
        ]),
    )
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None


# --- employment / experience -----------------------------------------------


@pytest.mark.parametrize("hh_id, expected", [
    ("full", "FULL_TIME"),
    ("part", "PART_TIME"),
    ("project", "CONTRACT"),
    ("probation", "CONTRACT"),
    ("volunteer", "CONTRACT"),
])
def test_employment_map_covers_known_ids(hh_id: str, expected: str) -> None:
    assert EMPLOYMENT_MAP[hh_id] == expected


@pytest.mark.parametrize("hh_id, expected", [
    ("noExperience", 0),
    ("between1And3", 1),
    ("between3And6", 3),
    ("moreThan6", 6),
])
def test_experience_map_covers_known_ids(hh_id: str, expected: int) -> None:
    assert EXPERIENCE_MAP[hh_id] == expected


def test_unknown_employment_leaves_field_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        json=_page([_item(employment_id="other-future")]),
    )
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert jobs[0].employment_type is None


# --- snippet / description --------------------------------------------------


def test_join_snippet_strips_highlight_tags() -> None:
    out = _join_snippet(
        "Опыт <highlighttext>Python</highlighttext> от 1 года.",
        "Разработка <highlighttext>backend</highlighttext> сервисов.",
    )
    assert out is not None
    assert "<highlighttext>" not in out
    assert "Python" in out
    assert "backend" in out


def test_join_snippet_both_none_returns_none() -> None:
    assert _join_snippet(None, None) is None


def test_join_snippet_one_side_only() -> None:
    out = _join_snippet("Только requirement.", None)
    assert out == "Только requirement."


# --- published_at parsing ---------------------------------------------------


def test_parse_published_at_handles_compact_offset() -> None:
    """hh.ru ships ``+0300`` (no colon), older fromisoformat versions
    rejected this — the scraper normalizes to ``+03:00``."""
    dt = _parse_published_at("2026-05-12T10:30:00+0300")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 12
    assert dt.hour == 10 and dt.minute == 30
    assert dt.utcoffset() is not None


def test_parse_published_at_none_returns_none() -> None:
    assert _parse_published_at(None) is None
    assert _parse_published_at("") is None


def test_parse_published_at_invalid_returns_none() -> None:
    assert _parse_published_at("yesterday") is None


# --- pagination -------------------------------------------------------------


def test_walks_all_pages_in_slice(httpx_mock) -> None:
    """Three pages in a slice; the scraper hits each in sequence and
    stops once ``pages`` is reached."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*page=0(&|$)"),
        json=_page([_item(job_id="a")], page=0, pages=3),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*page=1(&|$)"),
        json=_page([_item(job_id="b")], page=1, pages=3),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*page=2(&|$)"),
        json=_page([_item(job_id="c")], page=2, pages=3),
    )
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert {j.ats_id for j in jobs} == {"a", "b", "c"}


def test_stops_when_items_empty(httpx_mock) -> None:
    """``pages`` says 5 but page 1 ships ``items=[]`` — bail rather
    than walk to the end."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*page=0(&|$)"),
        json=_page([_item(job_id="a")], page=0, pages=5),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*page=1(&|$)"),
        json=_page([], page=1, pages=5),
    )
    # Pages 2-4 should NEVER be requested — httpx_mock errors on
    # un-stubbed calls.
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert {j.ats_id for j in jobs} == {"a"}


def test_deduplicates_across_slices(httpx_mock) -> None:
    """The same job can show up under two overlapping areas (e.g. the
    federal subject + the city). The scraper keeps the first copy."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        json=_page([_item(job_id="dup-1"), _item(job_id="solo-2")]),
        is_reusable=True,
    )
    jobs = HHRuScraper("ru", slice_by="area", areas=["1", "2"]).fetch()
    # Two areas × the same two items = 4 returned items but only 2 unique.
    assert {j.ats_id for j in jobs} == {"dup-1", "solo-2"}


# --- slicing ---------------------------------------------------------------


def test_slice_by_area_fans_out_per_code(httpx_mock) -> None:
    """``slice_by='area'`` queries once per area code. Each area gets
    its own item set; the scraper merges them."""
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*area=1(&|$)"),
        json=_page([_item(job_id="moscow-1")], page=0, pages=1),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*area=2(&|$)"),
        json=_page([_item(job_id="spb-1")], page=0, pages=1),
    )
    jobs = HHRuScraper("ru", slice_by="area", areas=["1", "2"]).fetch()
    assert {j.ats_id for j in jobs} == {"moscow-1", "spb-1"}


# --- error handling --------------------------------------------------------


def test_403_raises_with_proxy_hint(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        status_code=403,
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="geo-blocks"):
        HHRuScraper("ru", slice_by="none").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        status_code=500,
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        HHRuScraper("ru", slice_by="none").fetch()


def test_skips_items_missing_required_fields(httpx_mock) -> None:
    """Defensive: an item without ``id`` / ``alternate_url`` / ``name``
    is dropped silently — keeps the whole batch from blowing up over
    one malformed row."""
    good = _item(job_id="good-1")
    bad_no_id = {**_item(), "id": None}
    bad_no_url = {**_item(job_id="bad-2"), "alternate_url": None}
    httpx_mock.add_response(
        url=re.compile(r"^https://api\.hh\.ru/vacancies\?.*"),
        json=_page([good, bad_no_id, bad_no_url]),
    )
    jobs = HHRuScraper("ru", slice_by="none").fetch()
    assert {j.ats_id for j in jobs} == {"good-1"}
