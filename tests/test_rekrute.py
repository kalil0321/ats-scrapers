"""Tests for the Rekrute.com (Morocco) scraper.

Scope: HTML row slicing, soup-based field extraction (title /
company / location / posted_at / facets), title-suffix country
resolution, title→location split, pagination + dedup, retry/backoff,
and registry wiring. The httpx network path is exercised via
``MockTransport`` so we never hit the live site.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest

from jobhive.models import ATSType
from jobhive.scrapers import RekruteScraper, ScraperRegistry
from jobhive.scrapers import rekrute as r_mod

# --- HTML fixture builders --------------------------------------------


def _make_row(
    *,
    job_id: str = "182716",
    title: str = "Directeur de l'Hébergement",
    city: str = "Casablanca",
    country: str = "Maroc",
    href: str | None = None,
    company: str = "Auto Nejma Maroc S.A.",
    description: str = "Auto Nejma recrute un poste à Casablanca.",
    pub_date: str = "12/05/2026",
    sector: str | None = "Automobile / Motos / Cycles",
    function: str | None = "Administration des ventes / SAV",
    experience: str | None = "Junior (1 à 3 ans)",
    study_level: str | None = "Bac +5 et plus",
    contract: str | None = "CDI",
    teletravail: str | None = "Non",
) -> str:
    """Build one realistic ``<li class="post-id">`` block.

    Mirrors the live markup captured 2026-05-12: the row body holds a
    column-2 image anchor (recruiter logo), a column-10 details block
    with the title anchor, a description span, a publication-date
    ``<em>``, and a facet ``<ul>`` of sector/function/experience/etc.
    """
    if href is None:
        href = (
            f"/offre-emploi-{job_id}.html"
        )
    full_title = f"{title} | {city} ({country})" if city else title
    facets: list[str] = []
    if sector:
        facets.append(
            f'<li>Secteur d\'activité : <a href="/offres.html?sec=1">'
            f"{sector}</a></li>"
        )
    if function:
        facets.append(
            f'<li>Fonction : <a href="/offres.html?fn=1">'
            f"{function}</a></li>"
        )
    if experience:
        facets.append(
            f"<li>Expérience requise : "
            f'<a href="/offres.html?wx=1">{experience}</a></li>'
        )
    if study_level:
        facets.append(
            f"<li>Niveau d'étude demandé : "
            f'<a href="/offres.html?st=1">{study_level}</a></li>'
        )
    contract_line = ""
    if contract:
        contract_line = (
            f'<li>Type de contrat proposé : '
            f'<a href="/offres.html?ct=1">{contract}</a>'
        )
        if teletravail is not None:
            contract_line += f" - Télétravail : {teletravail}"
        contract_line += "</li>"
    if contract_line:
        facets.append(contract_line)
    facets_html = "\n".join(facets)
    return f"""
<li class="post-id" id="{job_id}">
  <div>
    <div class="col-sm-2 col-xs-12">
      <a href="/recruiter-page-{job_id}.html">
        <img src="/logo/{job_id}" width="115" alt="{company}"
             title="{company}" class="photo">
      </a>
    </div>
    <div class="col-sm-10 col-xs-12">
      <div class="section">
        <h2 style="width:90%">
          <a class="titreJob" href="{href}">{full_title}</a>
        </h2>
        <div class="holder">
          <div class="info">
            <img class="fa" src="/i/ai.svg">
            <span>{description}</span>
          </div>
          <em class="date"><i class="fa fa-clock-o"></i>
            Publication : du <span>{pub_date}</span>
            au <span>12/07/2026</span>
            | Postes proposés: <span>1</span>
          </em>
          <div class="info">
            <i class="fa fa-info-circle"></i>
            <ul style="display: block;">
              {facets_html}
            </ul>
          </div>
        </div>
      </div>
    </div>
  </div>
</li>
""".strip()


def _wrap_page(rows: list[str]) -> str:
    """Wrap rows in the live Rekrute result shell.

    Empty fixtures include an out-of-range pagination span, matching the
    site's real empty tail pages. Rowless shells without that marker are
    parse failures, not a legitimate stop signal.
    """
    rows_html = "\n".join(rows)
    result_count = len(rows)
    range_start = 1 if rows else 11
    range_end = result_count if rows else 10
    total = result_count if rows else 10
    return f"""<!DOCTYPE html>
<html lang="fr"><head><title>Offres d'emploi Maroc</title></head>
<body>
<div class="container">
  <span class="pages"><span>{range_start} - {range_end}</span> sur {total}</span>
  <ul class="job-list job-list2" id="post-data">
    {rows_html}
  </ul>
</div>
</body></html>"""


# --- httpx transport stub ---------------------------------------------


class _ScriptedTransport(httpx.MockTransport):
    """Replays canned responses keyed by request URL.

    ``p=N`` / ``o=N`` fragments match exact query-parameter values so
    pagination tests cannot accidentally match page 1 against page 10.
    Other fragments still fall back to substring matching.
    """

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
                if self._matches(url_fragment, request):
                    return response
            raise AssertionError(f"unexpected URL {request.url}")

        super().__init__(handler)

    @staticmethod
    def _matches(url_fragment: str, request: httpx.Request) -> bool:
        key, sep, expected = url_fragment.partition("=")
        if sep and key in request.url.params:
            return request.url.params.get(key) == expected
        return url_fragment in str(request.url)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport,
) -> None:
    """Replace ``httpx.AsyncClient`` so the scraper uses our transport."""
    real_ctor = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_ctor(*args, **kwargs)

    monkeypatch.setattr(r_mod.httpx, "AsyncClient", factory)


# --- Registry / construction ------------------------------------------


def test_registry_resolves_rekrute() -> None:
    assert ScraperRegistry.get(ATSType.REKRUTE) is RekruteScraper


def test_default_construction_works() -> None:
    s = RekruteScraper("any")
    assert s.company_slug == "any"
    assert s.max_pages > 0
    assert s.concurrency >= 1


def test_concurrency_floor_is_one() -> None:
    """Defensive: a misconfigured ``concurrency=0`` would deadlock the
    semaphore. The scraper bumps to 1."""
    s = RekruteScraper("any", concurrency=0)
    assert s.concurrency == 1


# --- Row slicing ------------------------------------------------------


def test_iter_rows_extracts_each_post() -> None:
    page = _wrap_page([_make_row(job_id="1"), _make_row(job_id="2")])
    rows = list(r_mod._iter_rows(page))
    assert [rid for rid, _ in rows] == ["1", "2"]


def test_iter_rows_empty_page() -> None:
    assert list(r_mod._iter_rows(_wrap_page([]))) == []


# --- Single-row parsing -----------------------------------------------


def test_parse_row_minimal_realistic() -> None:
    """End-to-end parse of a single canonical row pins the public
    field mapping the dataset relies on."""
    fetched = datetime(2026, 5, 12)
    row = _make_row(
        job_id="42",
        title="Brand Manager",
        city="Casablanca",
        company="Groupe Bel",
        description="Bel recherche un Brand Manager.",
        sector="Agroalimentaire",
        function="Marketing",
        experience="Intermédiaire (3 à 5 ans)",
        study_level="Bac +5 et plus",
        contract="CDI",
        teletravail="Hybride",
    )
    job = r_mod._parse_row("42", row, fetched_at=fetched)
    assert job is not None
    assert job.ats_type is ATSType.REKRUTE
    assert job.ats_id == "42"
    assert job.title == "Brand Manager"
    assert job.company == "Groupe Bel"
    assert job.location == "Casablanca"
    assert job.country_iso == "MA"
    assert job.region == "Africa"
    assert job.language == "fr"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "CDI"
    assert job.department == "Agroalimentaire"
    # "Hybride" is treated as remote-true (partial-remote roles still
    # qualify as "is_remote" in our schema).
    assert job.is_remote is True
    assert job.posted_at == datetime(2026, 5, 12, tzinfo=r_mod.UTC)
    assert job.fetched_at == fetched
    assert job.description is not None
    assert "Bel recherche" in job.description
    assert job.raw is not None
    assert job.raw["sector"] == "Agroalimentaire"
    assert job.raw["function"] == "Marketing"
    assert job.raw["experience_label"] == "Intermédiaire (3 à 5 ans)"
    assert job.raw["study_level"] == "Bac +5 et plus"
    assert job.raw["contract_label"] == "CDI"


def test_parse_row_teletravail_non_is_remote_false() -> None:
    """``Télétravail : Non`` is explicit signal — surface ``False``,
    not ``None``, so downstream filters can distinguish 'site says
    on-site' from 'site didn't say'."""
    row = _make_row(teletravail="Non")
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.is_remote is False


def test_parse_row_teletravail_missing_leaves_is_remote_none() -> None:
    row = _make_row(teletravail=None)
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.is_remote is None


@pytest.mark.parametrize(
    ("contract", "expected_enum"),
    [
        ("CDI", "FULL_TIME"),
        ("CDD", "TEMPORARY"),
        ("Stage", "INTERN"),
        ("Freelance", "CONTRACT"),
        ("Intérim", "TEMPORARY"),
        ("Temps partiel", "PART_TIME"),
    ],
)
def test_french_contract_maps_to_employment_type(
    contract: str, expected_enum: str,
) -> None:
    row = _make_row(contract=contract)
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.commitment == contract
    assert job.employment_type == expected_enum


def test_unknown_contract_label_kept_in_commitment_employment_none() -> None:
    """Custom contract labels (recruiter-specific) should round-trip
    in ``commitment`` even when the normalised enum doesn't cover
    them."""
    row = _make_row(contract="Contrat spécial 18 mois")
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.commitment == "Contrat spécial 18 mois"
    assert job.employment_type is None


def test_country_iso_defaults_to_morocco_when_suffix_missing() -> None:
    """No ``| City (Country)`` suffix? Rekrute is a Morocco board so
    default to MA rather than leave the field empty."""
    row = _make_row(city="", title="Senior Dev")
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.country_iso == "MA"
    assert job.region == "Africa"
    assert job.location is None


def test_country_iso_recognises_non_morocco_suffix() -> None:
    """Rekrute's small international section uses suffixes like
    ``(Tunisie)`` / ``(France)``. Map to the right ISO."""
    row = _make_row(city="Tunis", country="Tunisie")
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.country_iso == "TN"
    assert job.region == "Africa"
    assert job.location == "Tunis"


def test_country_iso_unknown_country_suffix_defaults_to_morocco() -> None:
    """Unknown country suffixes fall back to Rekrute's Morocco-board default."""
    row = _make_row(city="Atlantis", country="Atlantide")
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.country_iso == "MA"
    assert job.region == "Africa"


def test_parse_row_skips_when_title_anchor_missing() -> None:
    """A confidential / corrupted row without a titreJob anchor should
    be skipped, not fabricated."""
    row = (
        '<li class="post-id" id="bad">'
        '<div><h2>raw text only no anchor</h2></div></li>'
    )
    assert r_mod._parse_row("bad", row, fetched_at=datetime.now()) is None


def test_parse_row_placeholder_company_logo_becomes_unknown() -> None:
    """Some recruiters haven't uploaded a logo — Rekrute serves a
    placeholder with alt='Logo'. Don't surface ``company='Logo'``."""
    row = _make_row(company="Logo")
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.company == "Unknown"


def test_parse_row_uses_non_logo_company_text_fallback() -> None:
    """Rows without a logo image can still expose recruiter text."""
    row = _make_row(company="Logo").replace(
        '<img src="/logo/182716" width="115" alt="Logo"\n'
        '             title="Logo" class="photo">',
        'Société Sans Logo',
    )
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.company == "Société Sans Logo"


def test_invalid_pub_date_falls_back_to_none() -> None:
    """A garbage date mustn't crash the parser — drop the field."""
    row = _make_row(pub_date="99/99/9999")
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.posted_at is None


def test_absolute_href_is_kept_as_is() -> None:
    """Defensive: when Rekrute returns an absolute URL, don't
    double-prefix the host."""
    row = _make_row(href="https://www.rekrute.com/offre-emploi-abs-7.html")
    job = r_mod._parse_row("7", row, fetched_at=datetime.now())
    assert job is not None
    assert str(job.url) == "https://www.rekrute.com/offre-emploi-abs-7.html"


# --- End-to-end fetch (httpx mock) ------------------------------------


def test_fetch_walks_pages_until_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pagination stops as soon as a page returns zero rows."""
    page1 = _wrap_page([_make_row(job_id="1"), _make_row(job_id="2")])
    page2 = _wrap_page([_make_row(job_id="3")])
    empty = _wrap_page([])
    record: list[str] = []
    transport = _ScriptedTransport(
        [
            ("p=0", httpx.Response(200, text=page1)),
            ("p=1", httpx.Response(200, text=page2)),
            ("p=2", httpx.Response(200, text=empty)),
            ("p=3", httpx.Response(200, text=empty)),
            ("p=4", httpx.Response(200, text=empty)),
            ("p=5", httpx.Response(200, text=empty)),
        ],
        record=record,
    )
    _patch_client(monkeypatch, transport)
    jobs = RekruteScraper("any", max_pages=5, concurrency=1).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "3"]
    # First three requests are guaranteed; concurrency=1 + sequential
    # waves means we stop after the empty page.
    assert any("p=0" in u for u in record)
    assert any("p=1" in u for u in record)
    assert any("p=2" in u for u in record)


def test_fetch_dedupes_repeated_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sticky / featured rows show up on every page — surface once
    across the whole walk."""
    sticky = _make_row(job_id="999")
    page1 = _wrap_page([sticky, _make_row(job_id="1")])
    page2 = _wrap_page([sticky, _make_row(job_id="2")])
    empty = _wrap_page([])
    transport = _ScriptedTransport(
        [
            ("p=0", httpx.Response(200, text=page1)),
            ("p=1", httpx.Response(200, text=page2)),
            ("p=2", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = RekruteScraper("any", max_pages=5, concurrency=1).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "999"]


def test_fetch_respects_max_pages_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site that keeps returning fresh rows forever should still
    stop at the safety cap."""
    pages = [
        (f"p={i}", httpx.Response(200, text=_wrap_page(
            [_make_row(job_id=str(i + 100))],
        )))
        for i in range(20)
    ]
    transport = _ScriptedTransport(pages)
    _patch_client(monkeypatch, transport)
    jobs = RekruteScraper("any", max_pages=3, concurrency=1).fetch()
    assert len(jobs) == 3


def test_fetch_skips_5xx_page_without_stopping(
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_sleep: None,
) -> None:
    """A 5xx after exhausted retries skips that page but does not
    masquerade as the empty tail page."""
    page1 = _wrap_page([_make_row(job_id="1")])
    page3 = _wrap_page([_make_row(job_id="3")])
    empty = _wrap_page([])
    transport = _ScriptedTransport(
        [
            ("p=0", httpx.Response(200, text=page1)),
            ("p=1", httpx.Response(503)),
            ("p=2", httpx.Response(200, text=page3)),
            ("p=3", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = RekruteScraper("any", max_pages=4, concurrency=1).fetch()
    assert [j.ats_id for j in jobs] == ["1", "3"]


def test_fetch_skips_malformed_page_without_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken 200 page should not be treated as the empty tail page."""
    page1 = _wrap_page([_make_row(job_id="1")])
    malformed = "<html><body>maintenance</body></html>"
    page3 = _wrap_page([_make_row(job_id="3")])
    empty = _wrap_page([])
    transport = _ScriptedTransport(
        [
            ("p=0", httpx.Response(200, text=page1)),
            ("p=1", httpx.Response(200, text=malformed)),
            ("p=2", httpx.Response(200, text=page3)),
            ("p=3", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = RekruteScraper("any", max_pages=4, concurrency=1).fetch()
    assert [j.ats_id for j in jobs] == ["1", "3"]


def test_fetch_skips_rowless_in_range_shell_without_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell-only page within the reported result range is malformed,
    not the empty tail page."""
    page1 = _wrap_page([_make_row(job_id="1")])
    malformed_shell = """<!DOCTYPE html>
<html><body>
  <span class="pages"><span>11 - 20</span> sur 30</span>
  <ul class="job-list job-list2" id="post-data"></ul>
</body></html>"""
    page3 = _wrap_page([_make_row(job_id="3")])
    empty = _wrap_page([])
    transport = _ScriptedTransport(
        [
            ("p=0", httpx.Response(200, text=page1)),
            ("p=1", httpx.Response(200, text=malformed_shell)),
            ("p=2", httpx.Response(200, text=page3)),
            ("p=3", httpx.Response(200, text=empty)),
        ],
    )
    _patch_client(monkeypatch, transport)
    jobs = RekruteScraper("any", max_pages=4, concurrency=1).fetch()
    assert [j.ats_id for j in jobs] == ["1", "3"]


@pytest.fixture
def monkeypatch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``asyncio.sleep`` so retry-path tests don't pay
    wall-clock cost."""
    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(r_mod.asyncio, "sleep", _noop)


def test_url_uses_zero_based_p_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rekrute pagination is 0-indexed on the wire (``?p=0`` is page 1).
    The scraper translates 1-based logical pages at the URL boundary.
    """
    record: list[str] = []
    transport = _ScriptedTransport(
        [("p=0", httpx.Response(200, text=_wrap_page([])))],
        record=record,
    )
    _patch_client(monkeypatch, transport)
    RekruteScraper("any", max_pages=1, concurrency=1).fetch()
    assert any("p=0" in u for u in record)


# --- Edge cases on title parsing --------------------------------------


def test_title_with_multiple_pipes_keeps_only_last_segment_as_location() -> None:
    """``Job title | Sub-title | City (Maroc)`` — the location regex is
    greedy on the *final* ``|`` so the rest stays as title."""
    row = _make_row(
        title="Senior Dev | Backend Team",
        city="Casablanca",
        country="Maroc",
    )
    job = r_mod._parse_row("1", row, fetched_at=datetime.now())
    assert job is not None
    assert job.title == "Senior Dev | Backend Team"
    assert job.location == "Casablanca"
    assert job.country_iso == "MA"
