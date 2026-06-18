"""Tests for the Avito Maroc (avito.ma) scraper.

Scope: __NEXT_DATA__ JSON extraction, category-filter guard (drop
boosted ads from other categories), French relative-time parsing,
pagination + dedup, retry/backoff, and registry wiring. The httpx
network path is exercised via ``MockTransport`` so we never hit
the live site.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from jobhive.models import ATSType
from jobhive.scrapers import AvitoMarocScraper, ScraperRegistry
from jobhive.scrapers import avito_ma as a_mod

# --- Fixture builders -------------------------------------------------


def _make_ad(
    *,
    ad_id: str = "77081446",
    list_id: str = "57625044",
    subject: str = "Femme de ménage",
    description: str = "Service à domicile au Maroc.",
    href: str | None = None,
    seller_name: str = "2TG BUSINESS SERVICES",
    seller_type: str = "STORE",
    seller_id: str = "7373",
    location: str = "Oujda, Bd Hassan II",
    date_text: str = "il y a 7 minutes",
    cat_id: str = "6050",
    cat_name: str = "Centre d'appels",
    parent_id: str = "6200",
    parent_name: str = "Emploi",
    images: list[str] | None = None,
    is_premium: bool = False,
    is_shop: bool = False,
    is_urgent: bool = False,
) -> dict[str, Any]:
    """One realistic ad payload mirroring the shape captured from
    ``componentProps.ads.ads[*]`` in the live ``__NEXT_DATA__``."""
    if href is None:
        href = f"https://www.avito.ma/fr/x/centre_d_appels/Job_{list_id}.htm"
    if images is None:
        images = ["https://content.avito.ma/classifieds/images/x1"]
    return {
        "id": ad_id,
        "listId": list_id,
        "subject": subject,
        "description": description,
        "href": href,
        "location": location,
        "date": date_text,
        "category": {
            "id": cat_id,
            "name": cat_name,
            "formatted": f"{parent_name} - {cat_name}",
            "parent": {"id": parent_id, "name": parent_name},
        },
        "seller": {
            "id": seller_id,
            "name": seller_name,
            "type": seller_type,
        },
        "images": images,
        "isPremium": is_premium,
        "isShop": is_shop,
        "isUrgent": is_urgent,
    }


def _wrap_next_data(ads: list[dict[str, Any]], total: int | None = None) -> str:
    """Wrap a list of ads in just enough of the Next.js page envelope
    to mirror ``__NEXT_DATA__``."""
    blob = {
        "props": {
            "pageProps": {
                "componentProps": {
                    "ads": {
                        "ads": ads,
                        "totalListingAds": total if total is not None else len(ads),
                    },
                },
            },
        },
    }
    blob_json = json.dumps(blob, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="fr"><head><title>Emploi Maroc</title></head>
<body>
<div id="__next">…</div>
<script id="__NEXT_DATA__" type="application/json">{blob_json}</script>
</body></html>"""


# --- httpx transport stub ---------------------------------------------


class _ScriptedTransport(httpx.MockTransport):
    """URL-substring keyed response replay."""

    def __init__(
        self,
        scripts: list[tuple[str, httpx.Response]],
        record: list[str] | None = None,
    ) -> None:
        self._scripts = scripts
        self._record = record if record is not None else []

        def handler(request: httpx.Request) -> httpx.Response:
            self._record.append(str(request.url))
            for url_fragment, response in self._scripts:
                if url_fragment in str(request.url):
                    return response
            raise AssertionError(f"unexpected URL {request.url}")

        super().__init__(handler)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport,
) -> None:
    real_ctor = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_ctor(*args, **kwargs)

    monkeypatch.setattr(a_mod.httpx, "AsyncClient", factory)


# --- Registry / construction ------------------------------------------


def test_registry_resolves_avito_ma() -> None:
    assert ScraperRegistry.get(ATSType.AVITOMA) is AvitoMarocScraper


def test_default_construction_works() -> None:
    s = AvitoMarocScraper("any")
    assert s.company_slug == "any"
    assert s.max_pages > 0


def test_concurrency_floor_is_one() -> None:
    s = AvitoMarocScraper("any", concurrency=0)
    assert s.concurrency == 1


# --- __NEXT_DATA__ extraction -----------------------------------------


def test_extract_ads_returns_list() -> None:
    page = _wrap_next_data([_make_ad(), _make_ad(ad_id="2", list_id="22")])
    assert len(a_mod._extract_ads(page)) == 2


def test_extract_ads_missing_blob_raises() -> None:
    with pytest.raises(a_mod._AdsParseError):
        a_mod._extract_ads("<html>no script</html>")


def test_extract_ads_malformed_json_raises() -> None:
    """A page where Avito ships invalid JSON in the script tag must
    raise a parse error — distinct from a genuine empty last page — so
    pagination retries instead of truncating."""
    bad = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{this is not json'
        '</script></body></html>'
    )
    with pytest.raises(a_mod._AdsParseError):
        a_mod._extract_ads(bad)


def test_extract_ads_shape_changed_raises() -> None:
    """If Avito rearranges the JSON path, raise a parse error rather
    than silently returning ``[]`` and ending pagination."""
    page = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"shape":"changed"}}}'
        '</script>'
    )
    with pytest.raises(a_mod._AdsParseError):
        a_mod._extract_ads(page)


# --- Single-ad parsing -------------------------------------------------


def test_parse_ad_full_mapping() -> None:
    """End-to-end parse of a realistic Emploi ad. Pins the field
    mapping the public dataset relies on."""
    fetched = datetime(2026, 5, 12, 10, 0, 0)
    ad = _make_ad(
        ad_id="42",
        list_id="999",
        subject="Recherche commercial",
        description="Société cherche commercial dynamique.",
        seller_name="SALWA NASSER",
        location="Casablanca, 2 Mars",
        cat_name="Commercial",
        cat_id="6012",
        date_text="il y a 2 heures",
        is_shop=True,
        is_premium=True,
    )
    job = a_mod._parse_ad(ad, fetched_at=fetched)
    assert job is not None
    assert job.ats_type is ATSType.AVITOMA
    assert job.ats_id == "42"
    assert job.title == "Recherche commercial"
    assert job.company == "SALWA NASSER"
    assert job.location == "Casablanca, 2 Mars"
    assert job.country_iso == "MA"
    assert job.region == "Africa"
    assert job.language == "fr"
    assert job.department == "Emploi - Commercial"
    assert job.description is not None
    assert "commercial" in job.description.lower()
    assert job.posted_at == fetched - timedelta(hours=2)
    assert job.fetched_at == fetched
    assert job.raw is not None
    assert job.raw["seller_type"] == "STORE"
    assert job.raw["seller_id"] == "7373"
    assert job.raw["list_id"] == "999"
    assert job.raw["category_id"] == "6012"
    assert job.raw["is_shop"] is True
    assert job.raw["is_premium"] is True
    assert job.raw["image_count"] == 1


def test_parse_ad_drops_non_jobs_categories() -> None:
    """Boosted ads from other categories slip into the listing URL —
    the parent-id guard drops them so we don't surface laptops /
    cars as jobs."""
    ad = _make_ad(
        cat_id="5030",
        cat_name="Ordinateurs portables",
        parent_id="5000",
        parent_name="INFORMATIQUE ET MULTIMEDIA",
    )
    assert a_mod._parse_ad(ad, fetched_at=datetime.now()) is None


def test_parse_ad_keeps_top_level_emploi_category() -> None:
    """An ad posted at the Emploi root (``category.id == 6200``)
    rather than under a subcategory should still pass the filter."""
    ad = _make_ad(
        cat_id="6200",
        cat_name="Emploi",
        parent_id="",
        parent_name="",
    )
    job = a_mod._parse_ad(ad, fetched_at=datetime.now())
    assert job is not None
    assert job.country_iso == "MA"


def test_parse_ad_missing_subject_returns_none() -> None:
    ad = _make_ad(subject="")
    assert a_mod._parse_ad(ad, fetched_at=datetime.now()) is None


def test_parse_ad_missing_href_returns_none() -> None:
    ad = _make_ad(href="")
    assert a_mod._parse_ad(ad, fetched_at=datetime.now()) is None


def test_parse_ad_missing_seller_name_becomes_unknown() -> None:
    ad = _make_ad(seller_name="")
    job = a_mod._parse_ad(ad, fetched_at=datetime.now())
    assert job is not None
    assert job.company == "Unknown"


def test_parse_ad_long_description_truncated() -> None:
    """Avito occasionally serves a 10kB description block. The Job
    schema documents ``~10kB`` as the cap; we trim at 5k and append
    an ellipsis so the row stays compact in the dataset."""
    long_desc = "x" * 8000
    ad = _make_ad(description=long_desc)
    job = a_mod._parse_ad(ad, fetched_at=datetime.now())
    assert job is not None
    assert job.description is not None
    assert len(job.description) <= 5001
    assert job.description.endswith("…")


def test_parse_ad_relative_href_resolved_to_absolute() -> None:
    """Some payloads ship the href as a relative path. The scraper
    should produce a fully-qualified URL on the canonical site."""
    ad = _make_ad(href="/fr/x/y/Foo_123.htm")
    job = a_mod._parse_ad(ad, fetched_at=datetime.now())
    assert job is not None
    assert str(job.url).startswith("https://www.avito.ma/")


# --- French relative date parsing -------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_kwargs"),
    [
        ("il y a 5 minutes", {"minutes": 5}),
        ("il y a 1 minute", {"minutes": 1}),
        ("il y a 3 heures", {"hours": 3}),
        ("il y a 1 heure", {"hours": 1}),
        ("il y a 2 jours", {"days": 2}),
        ("il y a 1 jour", {"days": 1}),
        ("il y a 4 semaines", {"weeks": 4}),
    ],
)
def test_parse_relative_date_known_units(
    text: str, expected_kwargs: dict[str, int],
) -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    expected = now - timedelta(**expected_kwargs)
    assert a_mod._parse_relative_date(text, now=now) == expected


def test_parse_relative_date_months_approximated_at_30_days() -> None:
    """Avito's ``il y a 2 mois`` is coarse — we approximate 30 days
    per month so the timestamp stays plausible."""
    now = datetime(2026, 5, 12)
    out = a_mod._parse_relative_date("il y a 2 mois", now=now)
    assert out == now - timedelta(days=60)


def test_parse_relative_date_years_approximated_at_365_days() -> None:
    now = datetime(2026, 5, 12)
    out = a_mod._parse_relative_date("il y a 1 an", now=now)
    assert out == now - timedelta(days=365)


def test_parse_relative_date_unknown_phrase_returns_none() -> None:
    assert a_mod._parse_relative_date("hier", now=datetime.now()) is None
    assert a_mod._parse_relative_date(None, now=datetime.now()) is None
    assert a_mod._parse_relative_date("", now=datetime.now()) is None


# --- End-to-end fetch (httpx mock) ------------------------------------


def test_fetch_walks_pages_until_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pagination halts when a page yields zero ads."""
    page1 = _wrap_next_data(
        [_make_ad(ad_id="1", list_id="11"), _make_ad(ad_id="2", list_id="22")],
    )
    page2 = _wrap_next_data([_make_ad(ad_id="3", list_id="33")])
    empty = _wrap_next_data([])
    transport = _ScriptedTransport(
        [
            ("o=1", httpx.Response(200, text=page1)),
            ("o=2", httpx.Response(200, text=page2)),
            ("o=3", httpx.Response(200, text=empty)),
            ("o=4", httpx.Response(200, text=empty)),
            ("o=5", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = AvitoMarocScraper("any", max_pages=5, concurrency=1).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "3"]


def test_fetch_dedupes_repeated_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Premium / repeated ads should surface once across the walk."""
    sticky = _make_ad(ad_id="999", list_id="999")
    page1 = _wrap_next_data([sticky, _make_ad(ad_id="1", list_id="11")])
    page2 = _wrap_next_data([sticky, _make_ad(ad_id="2", list_id="22")])
    empty = _wrap_next_data([])
    transport = _ScriptedTransport(
        [
            ("o=1", httpx.Response(200, text=page1)),
            ("o=2", httpx.Response(200, text=page2)),
            ("o=3", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = AvitoMarocScraper("any", max_pages=5, concurrency=1).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "999"]


def test_fetch_respects_max_pages_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        (f"o={i + 1}", httpx.Response(200, text=_wrap_next_data(
            [_make_ad(ad_id=str(i + 100), list_id=str(i + 200))],
        )))
        for i in range(20)
    ]
    transport = _ScriptedTransport(pages)
    _patch_client(monkeypatch, transport)
    jobs = AvitoMarocScraper("any", max_pages=3, concurrency=1).fetch()
    assert len(jobs) == 3


def test_fetch_uses_emploi_url_not_offres_d_emploi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scraper must hit ``/fr/maroc/emploi`` (category-restricted)
    not the cross-category ``/fr/maroc/offres_d_emploi`` URL that
    mixes in boosted non-job ads."""
    record: list[str] = []
    transport = _ScriptedTransport(
        [("o=1", httpx.Response(200, text=_wrap_next_data([])))],
        record=record,
    )
    _patch_client(monkeypatch, transport)
    AvitoMarocScraper("any", max_pages=1, concurrency=1).fetch()
    assert any("/fr/maroc/emploi" in u for u in record)
    assert not any("offres_d_emploi" in u for u in record)


def test_fetch_drops_boosted_non_jobs_in_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the listing JSON includes a boosted non-job ad we drop
    it instead of surfacing it as a job."""
    job_ad = _make_ad(ad_id="J1", list_id="JL")
    boosted = _make_ad(
        ad_id="B1", list_id="BL",
        cat_id="5030", cat_name="Ordinateurs portables",
        parent_id="5000", parent_name="INFORMATIQUE ET MULTIMEDIA",
    )
    page1 = _wrap_next_data([boosted, job_ad])
    empty = _wrap_next_data([])
    transport = _ScriptedTransport(
        [
            ("o=1", httpx.Response(200, text=page1)),
            ("o=2", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = AvitoMarocScraper("any", max_pages=5, concurrency=1).fetch()
    assert [j.ats_id for j in jobs] == ["J1"]


@pytest.fixture
def monkeypatch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``asyncio.sleep`` so retry-path tests don't pay
    wall-clock cost."""
    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(a_mod.asyncio, "sleep", _noop)


def test_fetch_handles_5xx_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_sleep: None,
) -> None:
    """A 5xx after exhausted retries breaks the pagination loop but
    leaves already-collected jobs intact."""
    page1 = _wrap_next_data([_make_ad(ad_id="1", list_id="11")])
    transport = _ScriptedTransport(
        [
            ("o=1", httpx.Response(200, text=page1)),
            ("o=2", httpx.Response(503)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = AvitoMarocScraper("any", max_pages=5, concurrency=1).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_fetch_skips_malformed_page_without_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken 200 page should not be treated as the empty tail page."""
    page1 = _wrap_next_data([_make_ad(ad_id="1", list_id="11")])
    malformed = "<html><body>maintenance</body></html>"
    page3 = _wrap_next_data([_make_ad(ad_id="3", list_id="33")])
    empty = _wrap_next_data([])
    transport = _ScriptedTransport(
        [
            ("o=1", httpx.Response(200, text=page1)),
            ("o=2", httpx.Response(200, text=malformed)),
            ("o=3", httpx.Response(200, text=page3)),
            ("o=4", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = AvitoMarocScraper("any", max_pages=4, concurrency=1).fetch()
    assert [j.ats_id for j in jobs] == ["1", "3"]
