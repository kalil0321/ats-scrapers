"""Tests for the Bumeran / Navent (LATAM) scraper.

Bumeran sits behind Cloudflare and so uses ``httpcloak`` for transport
instead of ``httpx``. ``httpx_mock`` doesn't intercept httpcloak, so the
test suite stubs the two seams the scraper exposes:

- ``BumeranScraper._open_session`` — returns a sentinel "session"
- ``BumeranScraper._search_page`` — returns a canned API payload

Both are thin wrappers around the blocking httpcloak path, so faking
them lets us exercise pagination, multi-region wiring, and field
mapping without ever hitting the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import BumeranScraper, ScraperRegistry
from ats_scrapers.scrapers.bumeran import (
    PAGE_SIZE,
    REGIONS,
    _clean_description,
    _parse_datetime,
)

# --- fixtures ---------------------------------------------------------------


def _aviso(
    *,
    job_id: int = 1,
    titulo: str = "Analista de Datos",
    empresa: str = "Acme S.A.",
    localizacion: str = "Buenos Aires, Argentina",
    tipo_trabajo: str = "Full-time",
    modalidad: str = "Presencial",
    fecha: str = "11-05-2026 23:43:58",
    detalle: str = "Buscamos analista de datos con experiencia en SQL.",
    confidencial: bool = False,
    id_area: int = 23,
    id_subarea: int = 2569,
    id_empresa: int = 13480073,
    portal: str = "bumeran",
    cantidad_vacantes: int = 1,
) -> dict[str, Any]:
    """Build a single ``content[i]`` entry matching the live searchV2 shape."""
    return {
        "id": job_id,
        "titulo": titulo,
        "detalle": detalle,
        "aptoDiscapacitado": False,
        "idEmpresa": id_empresa,
        "empresa": empresa,
        "confidencial": confidencial,
        "logoURL": None,
        "validada": None,
        "empresaPro": False,
        "fechaHoraPublicacion": fecha,
        "fechaPublicacion": fecha.split(" ")[0] if " " in fecha else fecha,
        "fechaModificado": fecha,
        "planPublicacion": {"id": 1020, "nombre": "Aviso Talento"},
        "portal": portal,
        "tipoTrabajo": tipo_trabajo,
        "idPais": 1,
        "idArea": id_area,
        "idSubarea": id_subarea,
        "leido": None,
        "visitadoPorPostulante": None,
        "localizacion": localizacion,
        "cantidadVacantes": cantidad_vacantes,
        "guardado": None,
        "gptwUrl": None,
        "promedioEmpresa": None,
        "modalidadTrabajo": modalidad,
        "tienePreguntas": False,
        "salarioObligatorio": False,
        "altaRevisionPerfiles": False,
        "postulacionRapida": True,
        "tipoAviso": "talento",
    }


def _page_response(
    items: list[dict[str, Any]],
    *,
    page: int = 0,
    total: int | None = None,
    size: int | None = None,
) -> dict[str, Any]:
    """Build a full searchV2 envelope around the provided ``content`` items."""
    return {
        "number": page,
        "size": size if size is not None else len(items),
        "total": total if total is not None else len(items),
        "content": items,
        "filters": [],
        "filtersApplied": [],
        "totalSearched": total if total is not None else len(items),
        "homeList": None,
    }


@pytest.fixture
def patched_scraper(monkeypatch: pytest.MonkeyPatch):
    """Return a factory that builds a ``BumeranScraper`` with the
    network seams (``_open_session`` / ``_search_page``) stubbed to a
    page-indexed payload map. Any page index not in the map yields an
    empty page (mirrors how the live API responds past the last page).
    """

    def _factory(
        company_slug: str = "ar",
        *,
        pages: dict[int, dict[str, Any]] | None = None,
    ) -> BumeranScraper:
        pages = pages or {}
        # Always make httpcloak look installed so .fetch() doesn't
        # short-circuit to []. The Session itself is never used because
        # ``_search_page`` is also stubbed.
        monkeypatch.setattr(
            "ats_scrapers.scrapers.bumeran.find_spec", lambda _name: object(),
        )
        scraper = BumeranScraper(company_slug)
        monkeypatch.setattr(
            scraper, "_open_session", lambda: object(),
        )

        def fake_search(_session: Any, page: int) -> dict[str, Any]:
            return pages.get(page, _page_response([], page=page, total=0, size=0))

        monkeypatch.setattr(scraper, "_search_page", fake_search)
        return scraper

    return _factory


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_bumeran() -> None:
    assert ScraperRegistry.get(ATSType.BUMERAN) is BumeranScraper


def test_unknown_region_raises_company_not_found() -> None:
    with pytest.raises(CompanyNotFoundError):
        BumeranScraper("mx")  # MX isn't in the LATAM Navent footprint


def test_common_options_and_client_kind_are_accepted() -> None:
    scraper = BumeranScraper(
        "ar",
        include_descriptions=False,
        proxy="http://proxy.example:8080",
        client_kind="httpx",
    )
    assert scraper.include_descriptions is False
    assert scraper.proxy == "http://proxy.example:8080"
    assert scraper.client_kind == "httpx"


def test_invalid_client_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="client_kind"):
        BumeranScraper("ar", client_kind="browser")


@pytest.mark.parametrize(
    "slug,base_url,country_iso,site_id",
    [
        ("ar", "https://www.bumeran.com.ar", "AR", "BMAR"),
        ("pe", "https://www.bumeran.com.pe", "PE", "BMPE"),
        ("ec", "https://www.bumeran.com.ec", "EC", "BMEC"),
        ("ve", "https://www.bumeran.com.ve", "VE", "BMVE"),
        ("ar-zonajobs", "https://www.zonajobs.com.ar", "AR", "ZJAR"),
        ("ec-multitrabajos", "https://www.multitrabajos.com", "EC", "BMEC"),
    ],
)
def test_region_table_resolves(
    slug: str, base_url: str, country_iso: str, site_id: str,
) -> None:
    """Every documented slug maps to its known base / country / site-id.

    Catches accidental table edits — the slugs are part of the public
    interface (companies CSV references them by name)."""
    assert slug in REGIONS
    rec = REGIONS[slug]
    assert rec[0] == base_url
    assert rec[1] == site_id
    assert rec[2] == country_iso


# --- httpcloak gating -------------------------------------------------------


def test_fetch_returns_empty_when_httpcloak_missing(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """When httpcloak isn't installed, the publish pipeline must keep
    running. The scraper logs a warning and returns []."""
    import logging

    monkeypatch.setattr(
        "ats_scrapers.scrapers.bumeran.find_spec", lambda _name: None,
    )
    with caplog.at_level(logging.WARNING, logger="ats_scrapers.scrapers.bumeran"):
        jobs = BumeranScraper("ar").fetch()
    assert jobs == []
    assert any("httpcloak" in r.getMessage() for r in caplog.records)


def test_unsupported_region_returns_empty_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """``cl`` and ``pe-konzerta`` slugs are recognised so callers can keep
    a uniform company list, but the underlying stacks aren't on the
    shared searchV2 API. Scraper short-circuits with a warning."""
    import logging

    # Pretend httpcloak is installed so the early-return is purely about
    # the slug being unsupported.
    monkeypatch.setattr(
        "ats_scrapers.scrapers.bumeran.find_spec", lambda _name: object(),
    )
    with caplog.at_level(logging.WARNING, logger="ats_scrapers.scrapers.bumeran"):
        jobs = BumeranScraper("cl").fetch()
    assert jobs == []
    assert any("backend is not" in r.getMessage() for r in caplog.records)


# --- happy path: field mapping ----------------------------------------------


def test_parses_full_aviso_payload(patched_scraper) -> None:
    """Every populated Bumeran field maps to the right canonical Job slot.

    Anchors the contract on listings — adding/removing a mapping here is
    a breaking change for the published dataset."""
    item = _aviso(
        job_id=1118287655,
        titulo="Analista de Control de Calidad",
        empresa="Follow the Sun",
        localizacion="Grand Bourg, Buenos Aires",
        tipo_trabajo="Full-time",
        modalidad="Presencial",
        fecha="11-05-2026 23:43:58",
        detalle="&#x1f50e; Analista con experiencia en HPLC, UV e IR.",
    )
    scraper = patched_scraper("ar", pages={0: _page_response([item])})
    jobs = scraper.fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.BUMERAN
    assert j.ats_id == "1118287655"
    assert j.title == "Analista de Control de Calidad"
    assert j.company == "Follow the Sun"
    assert str(j.url) == (
        "https://www.bumeran.com.ar/empleos/aviso-1118287655.html"
    )
    assert j.location == "Grand Bourg, Buenos Aires"
    assert j.country_iso == "AR"
    assert j.region == "South America"
    assert j.language == "es"
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Full-time"
    assert j.is_remote is False  # Presencial
    # Description: HTML entities decoded, whitespace collapsed.
    assert j.description is not None
    assert "🔎" in j.description or "Analista" in j.description
    # raw carries Navent-specific fields the canonical schema can't hold.
    assert j.raw is not None
    assert j.raw["idArea"] == 23
    assert j.raw["portal"] == "bumeran"
    assert j.raw["country_alias"] == "ar"
    assert j.raw["site_id"] == "BMAR"
    # posted_at parsed from DD-MM-YYYY HH:MM:SS
    assert j.posted_at is not None
    assert j.posted_at.year == 2026
    assert j.posted_at.month == 5
    assert j.posted_at.day == 12
    assert j.posted_at.tzinfo is not None


def test_remoto_modalidad_marks_is_remote_true(patched_scraper) -> None:
    item = _aviso(modalidad="Remoto")
    scraper = patched_scraper("pe", pages={0: _page_response([item])})
    jobs = scraper.fetch()
    assert jobs[0].is_remote is True


def test_hybrid_modalidad_leaves_is_remote_unset(patched_scraper) -> None:
    """``Híbrido`` postings could be either — defer to the downstream
    enrichment instead of guessing."""
    item = _aviso(modalidad="Híbrido")
    scraper = patched_scraper("ar", pages={0: _page_response([item])})
    jobs = scraper.fetch()
    assert jobs[0].is_remote is None


def test_confidential_empty_company_maps_to_confidencial(
    patched_scraper,
) -> None:
    """Confidential listings have ``empresa=""``; emit "Confidencial"
    rather than letting the row default to a numeric employer id."""
    item = _aviso(empresa="", confidencial=True)
    scraper = patched_scraper("ar", pages={0: _page_response([item])})
    jobs = scraper.fetch()
    assert jobs[0].company == "Confidencial"


def test_pasantia_tipo_trabajo_maps_to_intern(patched_scraper) -> None:
    item = _aviso(tipo_trabajo="Pasantía")
    scraper = patched_scraper("ar", pages={0: _page_response([item])})
    jobs = scraper.fetch()
    assert jobs[0].employment_type == "INTERN"


# --- multi-country wiring ---------------------------------------------------


def test_zonajobs_country_iso_and_portal(patched_scraper) -> None:
    """zonajobs.com.ar lives on the AR backend tenant — same currency /
    country, but the ``portal`` field on the response is ``zonajobs``
    and the URL we synthesize points at zonajobs.com.ar."""
    item = _aviso(portal="zonajobs")
    scraper = patched_scraper("ar-zonajobs", pages={0: _page_response([item])})
    jobs = scraper.fetch()
    assert jobs[0].country_iso == "AR"
    assert str(jobs[0].url).startswith("https://www.zonajobs.com.ar/")
    assert jobs[0].raw is not None
    assert jobs[0].raw["country_alias"] == "ar-zonajobs"
    assert jobs[0].raw["site_id"] == "ZJAR"


def test_multitrabajos_uses_ec_country(patched_scraper) -> None:
    """multitrabajos.com is the EC alt brand — country_iso must follow."""
    item = _aviso()
    scraper = patched_scraper(
        "ec-multitrabajos", pages={0: _page_response([item])},
    )
    jobs = scraper.fetch()
    assert jobs[0].country_iso == "EC"
    assert str(jobs[0].url).startswith("https://www.multitrabajos.com/")
    assert jobs[0].raw is not None
    assert jobs[0].raw["site_id"] == "BMEC"


# --- pagination -------------------------------------------------------------


def test_paginates_until_total_reached(patched_scraper) -> None:
    """The first page reports ``total`` and ``size``; subsequent pages
    are walked until the math says we've covered every row."""
    page0 = _page_response(
        [_aviso(job_id=i) for i in range(PAGE_SIZE)],
        page=0, total=250, size=PAGE_SIZE,
    )
    page1 = _page_response(
        [_aviso(job_id=PAGE_SIZE + i) for i in range(PAGE_SIZE)],
        page=1, total=250, size=PAGE_SIZE,
    )
    page2 = _page_response(
        [_aviso(job_id=2 * PAGE_SIZE + i) for i in range(50)],
        page=2, total=250, size=50,
    )
    scraper = patched_scraper(
        "ar", pages={0: page0, 1: page1, 2: page2},
    )
    jobs = scraper.fetch()
    assert len(jobs) == 250
    assert {int(j.ats_id) for j in jobs} == set(range(250))


def test_dedupes_overlapping_pages(patched_scraper) -> None:
    """A row that appears on consecutive pages must collapse on ats_id —
    Navent's listing isn't strictly stable across rapid pagination."""
    page0 = _page_response(
        [_aviso(job_id=i) for i in range(5)],
        page=0, total=8, size=5,
    )
    page1 = _page_response(
        [_aviso(job_id=i) for i in (3, 4, 5, 6, 7)],
        page=1, total=8, size=5,
    )
    scraper = patched_scraper("ar", pages={0: page0, 1: page1})
    jobs = scraper.fetch()
    assert len(jobs) == 8
    assert len({j.ats_id for j in jobs}) == 8


def test_single_page_when_total_fits(patched_scraper) -> None:
    """``total <= size`` should not trigger a second fetch."""
    fetches: list[int] = []

    def _build(monkey, scraper: BumeranScraper) -> None:
        original = scraper._search_page

        def _spy(session: Any, page: int) -> dict[str, Any]:
            fetches.append(page)
            return original(session, page)

        monkey.setattr(scraper, "_search_page", _spy)

    page0 = _page_response(
        [_aviso(job_id=i) for i in range(3)], page=0, total=3, size=3,
    )
    scraper = patched_scraper("ar", pages={0: page0})

    # The factory already monkey-patched _search_page; wrap it once more
    # to spy on call counts.
    with pytest.MonkeyPatch.context() as mp:
        _build(mp, scraper)
        jobs = scraper.fetch()
    assert len(jobs) == 3
    assert fetches == [0]


def test_max_pages_caps_pagination(patched_scraper) -> None:
    """The ``max_pages`` knob protects against runaway scrapes when the
    API reports a wildly inflated ``total``."""
    page0 = _page_response(
        [_aviso(job_id=i) for i in range(2)],
        page=0, total=10_000, size=2,
    )
    page1 = _page_response(
        [_aviso(job_id=2 + i) for i in range(2)],
        page=1, total=10_000, size=2,
    )
    scraper = BumeranScraper("ar", max_pages=2)
    # Re-apply the same stubs the factory uses.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "ats_scrapers.scrapers.bumeran.find_spec", lambda _name: object(),
        )
        mp.setattr(scraper, "_open_session", lambda: object())

        pages = {0: page0, 1: page1}

        requested: list[int] = []

        def fake(_s: Any, p: int) -> dict[str, Any]:
            requested.append(p)
            return pages.get(p, _page_response([], page=p, total=0, size=0))

        mp.setattr(scraper, "_search_page", fake)
        jobs = scraper.fetch()
    assert len(jobs) == 4
    assert requested == [0, 1]


# --- defensive parsing ------------------------------------------------------


def test_skips_items_missing_id_or_title(patched_scraper) -> None:
    page0 = _page_response([
        _aviso(job_id=1, titulo="Good"),
        {"id": 2, "empresa": "no-title", "detalle": ""},  # no title
        {"titulo": "no-id"},  # no id
    ])
    scraper = patched_scraper("ar", pages={0: page0})
    jobs = scraper.fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_falls_back_for_unparsable_date(patched_scraper) -> None:
    """If the API ships a weird date, the row still gets emitted with
    ``posted_at=None`` rather than crashing the whole page."""
    item = _aviso(fecha="not-a-date")
    scraper = patched_scraper("ar", pages={0: _page_response([item])})
    jobs = scraper.fetch()
    assert jobs[0].posted_at is None


# --- helpers ----------------------------------------------------------------


def test_clean_description_decodes_entities_and_trims() -> None:
    assert _clean_description("Hola &amp; <b>mundo</b>  ") == "Hola & mundo"


def test_clean_description_returns_none_for_empty() -> None:
    assert _clean_description("") is None
    assert _clean_description("   ") is None
    assert _clean_description(None) is None


def test_clean_description_truncates_at_10k() -> None:
    out = _clean_description("x" * 20_000)
    assert out is not None
    assert len(out) == 10_000


def test_parse_datetime_accepts_full_and_date_only() -> None:
    from zoneinfo import ZoneInfo

    timezone = ZoneInfo("America/Argentina/Buenos_Aires")
    full = _parse_datetime("11-05-2026 23:43:58", timezone=timezone)
    assert full is not None
    assert (full.year, full.month, full.day) == (2026, 5, 12)
    assert (full.hour, full.minute, full.second) == (2, 43, 58)

    date_only = _parse_datetime("11-05-2026", timezone=timezone)
    assert date_only is not None
    assert (date_only.year, date_only.month, date_only.day) == (2026, 5, 11)
    assert (date_only.hour, date_only.minute, date_only.second) == (3, 0, 0)


def test_parse_datetime_returns_none_for_garbage() -> None:
    from zoneinfo import ZoneInfo

    timezone = ZoneInfo("America/Lima")
    assert _parse_datetime("", timezone=timezone) is None
    assert _parse_datetime(None, timezone=timezone) is None
    assert _parse_datetime("2026-05-11", timezone=timezone) is None
    assert _parse_datetime(12345, timezone=timezone) is None


# --- error handling ---------------------------------------------------------


def test_search_page_raises_on_persistent_5xx(monkeypatch) -> None:
    """Real server failures should surface, not silently emit []."""
    import ats_scrapers.scrapers.bumeran as bm

    monkeypatch.setattr(bm, "find_spec", lambda _name: object())
    monkeypatch.setattr(bm, "MAX_RETRIES", 2)
    monkeypatch.setattr(bm, "RETRY_BASE_DELAY", 0.0)

    class FakeResp:
        def __init__(self, status: int):
            self.status_code = status
            self.text = "boom"
            self.content = b"boom"

    class FakeSession:
        def get(self, url: str, timeout: float) -> Any:
            return FakeResp(200)

        def post(self, url: str, *, headers: Any, json: Any, timeout: float) -> Any:
            return FakeResp(500)

    scraper = BumeranScraper("ar")
    monkeypatch.setattr(scraper, "_open_session", lambda: FakeSession())
    # Use the real _search_page so the retry/raise path is exercised end
    # to end via the FakeSession above.
    with pytest.raises(ScraperError):
        scraper.fetch()


def test_search_page_rejects_non_object_json() -> None:
    class FakeResponse:
        status_code = 200
        text = "[]"
        content = b"[]"

    class FakeSession:
        def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    with pytest.raises(ScraperError, match="expected object"):
        BumeranScraper("ar")._search_page(FakeSession(), 0)
