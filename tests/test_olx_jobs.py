"""Tests for the OLX Jobs (multi-country classifieds) scraper.

Pin the contract for:

* registry wiring (``ATSType.OLX_JOBS`` → ``OlxJobsScraper``),
* per-country selection via ``company_slug``,
* the v1 offers endpoint cursor walk (``links.next.href`` → next URL),
* the param-tree extraction (salary, employment type, workplace, industry),
* defensive parsing (missing user.company_name, malformed map, 400 mid-walk).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import OlxJobsScraper, ScraperRegistry

_API_RE = re.compile(r"^https://www\.olx\.[a-z]+/api/v1/offers")
_PL_RE = re.compile(r"^https://www\.olx\.pl/api/v1/offers")
_UA_RE = re.compile(r"^https://www\.olx\.ua/api/v1/offers")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.olx_jobs as ox
    monkeypatch.setattr(ox, "MAX_RETRIES", 1)
    monkeypatch.setattr(ox, "RETRY_BASE_DELAY", 0.0)


def _offer(
    *,
    offer_id: int = 989010925,
    title: str = "Senior Engineer",
    url: str = "https://www.olx.pl/oferta/praca/foo-CID4-ID1.html",
    user_name: str = "Rafał",
    company_name: str = "",
    city: str = "Kórnik",
    region: str = "Wielkopolskie",
    district: str | None = None,
    lat: float | None = 52.24811,
    lon: float | None = 17.08378,
    description: str = "<p>Hello <b>world</b></p>",
    created_time: str = "2026-04-02T18:09:11+02:00",
    business: bool = False,
    params: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an OLX offer payload — a trimmed-down version of what the
    live API returns. Field names match the v1 contract verbatim so
    parser drift is caught in tests."""
    loc: dict[str, Any] = {
        "city": {"id": 1, "name": city, "normalized_name": city.lower()},
        "region": {"id": 1, "name": region, "normalized_name": region.lower()},
    }
    if district:
        loc["district"] = {"id": 1, "name": district}
    return {
        "id": offer_id,
        "title": title,
        "url": url,
        "description": description,
        "created_time": created_time,
        "last_refresh_time": "2026-05-11T22:56:39+02:00",
        "valid_to_time": "2026-06-10T22:56:36+02:00",
        "business": business,
        "user": {
            "id": 42,
            "name": user_name,
            "company_name": company_name,
            "uuid": "abc-def",
        },
        "location": loc,
        "map": {"lat": lat, "lon": lon, "show_detailed": False, "zoom": 12},
        "params": params or [],
        "category": {"id": 2509, "type": "job"},
        "status": "active",
    }


def _page(
    items: list[dict[str, Any]],
    *,
    next_url: str | None = None,
) -> dict[str, Any]:
    links: dict[str, Any] = {
        "self": {"href": "https://www.olx.pl/api/v1/offers?category_id=4&offset=0&limit=40"},
        "first": {"href": "https://www.olx.pl/api/v1/offers?category_id=4&offset=0&limit=40"},
    }
    if next_url is not None:
        # OLX wraps ``next`` as ``{"href": "..."}`` — preserve that shape.
        links["next"] = {"href": next_url}
    return {
        "data": items,
        "links": links,
        "metadata": {"total_elements": len(items), "visible_total_count": len(items)},
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_olx_jobs() -> None:
    assert ScraperRegistry.get(ATSType.OLX_JOBS) is OlxJobsScraper


def test_rejects_unknown_region() -> None:
    with pytest.raises(ScraperError):
        OlxJobsScraper("zz")


def test_rejects_comma_only_region_slug() -> None:
    with pytest.raises(ScraperError):
        OlxJobsScraper(",,")


def test_all_keyword_enumerates_every_region() -> None:
    scraper = OlxJobsScraper("all")
    codes = {r.code for r in scraper.regions}
    assert codes == set(OlxJobsScraper.SUPPORTED_REGIONS)


def test_comma_separated_picks_subset() -> None:
    scraper = OlxJobsScraper("pl,ua")
    assert [r.code for r in scraper.regions] == ["pl", "ua"]


def test_comma_separated_regions_are_deduplicated() -> None:
    scraper = OlxJobsScraper("pl,ua,pl")
    assert [r.code for r in scraper.regions] == ["pl", "ua"]


# --- happy path -------------------------------------------------------------


def test_parses_full_offer_payload(httpx_mock) -> None:
    """One PL offer → one Job; verify every populated field maps to the
    right v1 location."""
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([
            _offer(
                offer_id=12345,
                title="Doradca Klienta",
                url="https://www.olx.pl/oferta/praca/foo-CID4-ID14WMW7.html",
                company_name="Acme Sp. z o.o.",
                params=[
                    {
                        "key": "salary",
                        "value": {
                            "from": 5000,
                            "to": 8000,
                            "type": "monthly",
                            "currency": "PLN",
                            "gross": True,
                        },
                    },
                    {
                        "key": "type",
                        "value": {"key": "fulltime", "label": "Pełny etat"},
                    },
                    {
                        "key": "workplace",
                        "value": {
                            "key": ["on_site"],
                            "label": "W siedzibie firmy",
                        },
                    },
                    {
                        "key": "industry",
                        "value": {
                            "key": "ind_re",
                            "label": "Nieruchomości, budownictwo",
                        },
                    },
                ],
            )
        ]),
    )

    jobs = OlxJobsScraper("pl").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.OLX_JOBS
    assert j.ats_id == "pl:12345"
    assert j.global_id == "olx_jobs:pl:12345"
    assert j.title == "Doradca Klienta"
    # ``company_name`` beats ``user.name`` when both are set.
    assert j.company == "Acme Sp. z o.o."
    assert j.country_iso == "PL"
    assert j.region == "Europe"
    assert j.language == "pl"
    assert j.location == "Kórnik, Wielkopolskie"
    assert j.salary_currency == "PLN"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 5000
    assert j.salary_max == 8000
    assert j.salary_summary == "5000 – 8000 PLN / month"
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Pełny etat"
    assert j.department == "Nieruchomości, budownictwo"
    # HTML tags stripped, entities unescaped.
    assert j.description == "Hello world"
    assert j.lat == pytest.approx(52.24811)
    assert j.lon == pytest.approx(17.08378)
    assert j.is_remote is None  # workplace=on_site → leave None (not False)
    assert j.posted_at is not None
    assert j.posted_at.year == 2026
    assert j.fetched_at.tzinfo is not None
    assert j.raw is not None
    assert j.raw["region"] == "pl"
    assert j.raw["category_id"] == 2509


# --- pagination -------------------------------------------------------------


def test_paginates_via_links_next_href(httpx_mock) -> None:
    """v1 offers uses cursor-style pagination — ``links.next.href`` ferries
    the next URL. Walk until null."""
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page(
            [_offer(offer_id=i, url=f"https://www.olx.pl/o/{i}") for i in range(40)],
            next_url="https://www.olx.pl/api/v1/offers?category_id=4&offset=40&limit=40",
        ),
    )
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page(
            [_offer(offer_id=i, url=f"https://www.olx.pl/o/{i}") for i in range(40, 60)],
            next_url=None,
        ),
    )

    jobs = OlxJobsScraper("pl").fetch()
    assert len(jobs) == 60
    assert {j.ats_id for j in jobs} == {f"pl:{i}" for i in range(60)}


def test_same_raw_id_across_regions_is_kept_distinct(httpx_mock) -> None:
    """A single offer id is a *different* listing on two OLX properties
    (id 1 on olx.pl is unrelated to id 1 on olx.ua). The region prefix
    keeps them distinct so cross-region collisions don't silently drop
    real jobs. Within one region, a re-listed id must still collapse."""
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([
            _offer(offer_id=1, url="https://www.olx.pl/o/1"),
            # same id re-listed within the PL walk → must dedupe away.
            _offer(offer_id=1, url="https://www.olx.pl/o/1"),
        ]),
    )
    httpx_mock.add_response(
        url=_UA_RE,
        json=_page([_offer(offer_id=1, url="https://www.olx.ua/o/1")]),
    )
    jobs = OlxJobsScraper("pl,ua").fetch()
    assert len(jobs) == 2
    assert {j.ats_id for j in jobs} == {"pl:1", "ua:1"}


# --- field handling ---------------------------------------------------------


def test_company_falls_back_to_user_name_when_company_name_blank(httpx_mock) -> None:
    """Individual posters have ``company_name=""`` and only ``user.name``
    (often a first name) — fall back to that rather than emitting an
    empty company."""
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([_offer(offer_id=1, user_name="Sebastian", company_name="")]),
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert jobs[0].company == "Sebastian"


def test_company_is_unknown_when_both_blank(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([_offer(offer_id=1, user_name="", company_name="")]),
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert jobs[0].company == "Unknown"


def test_remote_workplace_sets_is_remote_true(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([
            _offer(
                offer_id=1,
                params=[{
                    "key": "workplace",
                    "value": {"key": ["remote"], "label": "Zdalna"},
                }],
            )
        ]),
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert jobs[0].is_remote is True


def test_null_island_latlon_is_dropped(httpx_mock) -> None:
    """OLX's ``map`` sometimes ships ``(0, 0)`` as a geocoding-failed
    sentinel; we must not propagate that as a real coordinate."""
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([_offer(offer_id=1, lat=0, lon=0)]),
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert jobs[0].lat is None
    assert jobs[0].lon is None


def test_salary_without_bounds_is_skipped(httpx_mock) -> None:
    """A salary param with ``from=null, to=null`` is a visibility flag,
    not an actual range — don't emit a phantom salary row."""
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([
            _offer(
                offer_id=1,
                params=[{
                    "key": "salary",
                    "value": {
                        "from": None,
                        "to": None,
                        "type": "monthly",
                        "currency": "PLN",
                    },
                }],
            )
        ]),
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None
    assert jobs[0].salary_currency is None


def test_salary_with_only_max_emits_up_to_summary(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([
            _offer(
                offer_id=1,
                params=[{
                    "key": "salary",
                    "value": {
                        "from": None,
                        "to": 32,
                        "type": "hourly",
                        "currency": "PLN",
                    },
                }],
            )
        ]),
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert jobs[0].salary_max == 32
    assert jobs[0].salary_period == "HOUR"
    assert jobs[0].salary_summary == "up to 32 PLN / hour"


def test_skips_offers_missing_id_or_title_or_url(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([
            _offer(offer_id=1, title="Good"),
            {"id": 2, "title": "No URL", "url": ""},
            {"id": 3, "url": "https://x/3"},  # no title
            {"title": "No id", "url": "https://x/4"},
        ]),
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert [j.ats_id for j in jobs] == ["pl:1"]


# --- error handling ---------------------------------------------------------


def test_400_mid_walk_terminates_cleanly(httpx_mock) -> None:
    """OLX returns 400 once offset > 1000. The scraper must treat that as
    'no more data' rather than crashing the entire region scrape — we
    keep whatever we collected up to that point."""
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page(
            [_offer(offer_id=1)],
            next_url="https://www.olx.pl/api/v1/offers?category_id=4&offset=1500&limit=40",
        ),
    )
    httpx_mock.add_response(
        url=_PL_RE,
        status_code=400,
        json={"error": {"status": 400, "detail": "offset out of range"}},
    )
    jobs = OlxJobsScraper("pl").fetch()
    assert len(jobs) == 1


def test_400_before_offset_cap_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_PL_RE,
        status_code=400,
        json={"error": {"status": 400, "detail": "bad category"}},
    )
    with pytest.raises(ScraperError, match="400"):
        OlxJobsScraper("pl").fetch()


def test_multi_region_keeps_partial_results_when_one_region_fails(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_PL_RE,
        json=_page([_offer(offer_id=1, url="https://www.olx.pl/o/1")]),
    )
    httpx_mock.add_response(url=_UA_RE, status_code=500)

    jobs = OlxJobsScraper("pl,ua").fetch()

    assert [j.ats_id for j in jobs] == ["pl:1"]


def test_multi_region_raises_when_every_region_fails(httpx_mock) -> None:
    httpx_mock.add_response(url=_PL_RE, status_code=500)
    httpx_mock.add_response(url=_UA_RE, status_code=500)

    with pytest.raises(ScraperError, match="every requested region"):
        OlxJobsScraper("pl,ua").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        OlxJobsScraper("pl").fetch()


def test_non_json_response_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=200, content=b"<html>oops</html>")
    with pytest.raises(ScraperError):
        OlxJobsScraper("pl").fetch()
