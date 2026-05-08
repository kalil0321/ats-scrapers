"""Tests for the JazzHR scraper.

JazzHR has no JSON API — every tenant serves a single HTML listing at
`/apply/jobs`. Some tenants are Cloudflare-protected and 403 plain httpx;
we fall back to httpcloak via `client_kind="auto"` (default).

These tests pin:

1. HTML row parsing (title, id, location, department)
2. Whitespace + entity handling
3. Retry behaviour
4. Cloudflare/WAF fallback to httpcloak (default `auto`)
5. Pinned `client_kind` modes (`httpx`, `httpcloak`)
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import JazzHRScraper, ScraperRegistry, get_scraper


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.jazzhr as jh
    monkeypatch.setattr(jh, "MAX_RETRIES", 1)
    monkeypatch.setattr(jh, "RETRY_BASE_DELAY", 0.0)


# Per-job detail enrichment fires after the listing parse; tests that
# don't care about it leave those calls unmocked.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


URL = "https://acme.applytojob.com/apply/jobs"


def _row(
    *,
    job_id: str,
    title: str = "Senior Engineer",
    department: str | None = "Engineering",
    location: str | None = "Berlin",
) -> str:
    """Build one JazzHR job row in the same shape live tenants emit."""
    dept_html = (
        f'<br /><span class="resumator_department">{department}</span>'
        if department else ""
    )
    return (
        f'<tr id="row_job_{job_id}" class="resumator_even_row">'
        f'<td>'
        f'<a class="job_title_link" href="/apply/jobs/details/{job_id}?&">{title}</a>'
        f'{dept_html}'
        f'</td>'
        f'<td>{location or ""}</td>'
        f'</tr>'
    )


def _listing(rows: list[str]) -> str:
    return (
        '<html><body>'
        '<div id="job_listings">'
        '<table id="jobs_table" class="menu_table">'
        '<tbody>'
        '<tr><th>Position</th><th>Location</th></tr>'
        + "".join(rows) +
        '</tbody></table>'
        '</div>'
        '</body></html>'
    )


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_jazzhr() -> None:
    assert ScraperRegistry.get(ATSType.JAZZHR) is JazzHRScraper


def test_get_scraper_by_string_returns_jazzhr() -> None:
    s = get_scraper("jazzhr", "acme")
    assert isinstance(s, JazzHRScraper)
    assert s.company_slug == "acme"


# --- Construction -----------------------------------------------------------


def test_default_client_kind_is_auto() -> None:
    """Default must be `auto` — most tenants work fine on httpx, but some
    are Cloudflare-protected. Auto-fallback keeps the common case fast."""
    s = JazzHRScraper("acme")
    assert s.client_kind == "auto"


def test_client_kind_settable() -> None:
    assert JazzHRScraper("acme", client_kind="httpx").client_kind == "httpx"
    assert JazzHRScraper("acme", client_kind="httpcloak").client_kind == "httpcloak"


# --- Happy path -------------------------------------------------------------


def test_parses_basic_listing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        text=_listing([
            _row(job_id="ABC123", title="Backend", location="Berlin"),
            _row(job_id="DEF456", title="Frontend", location="Remote"),
        ]),
    )
    jobs = JazzHRScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["ABC123", "DEF456"]
    assert jobs[0].title == "Backend"
    assert jobs[0].location == "Berlin"
    assert jobs[0].department == "Engineering"
    assert jobs[0].ats_type is ATSType.JAZZHR
    assert jobs[0].company == "acme"
    assert str(jobs[0].url) == "https://acme.applytojob.com/apply/jobs/details/ABC123"


def test_returns_empty_for_listing_with_no_rows(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_listing([]))
    assert JazzHRScraper("acme").fetch() == []


def test_dedupes_rows_with_same_id(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_listing([
        _row(job_id="A1", title="X"),
        _row(job_id="A1", title="X duplicate listing"),
    ]))
    jobs = JazzHRScraper("acme").fetch()
    assert len(jobs) == 1


def test_handles_missing_department(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_listing([
        _row(job_id="A1", title="X", department=None, location="NYC"),
    ]))
    jobs = JazzHRScraper("acme").fetch()
    assert jobs[0].department is None


def test_handles_missing_location(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_listing([
        _row(job_id="A1", title="X", location=None),
    ]))
    jobs = JazzHRScraper("acme").fetch()
    assert jobs[0].location is None


def test_skips_row_without_title_link(httpx_mock) -> None:
    """Some malformed rows just have placeholder content. They must be
    skipped without raising."""
    bad_row = (
        '<tr id="row_job_BAD" class="resumator_even_row">'
        '<td>some non-anchor text</td><td>NYC</td>'
        '</tr>'
    )
    httpx_mock.add_response(
        url=URL, text=_listing([bad_row, _row(job_id="GOOD", title="Real")])
    )
    jobs = JazzHRScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["GOOD"]


# --- Whitespace + entity handling ------------------------------------------


def test_decodes_html_entities_in_title(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_listing([
        _row(job_id="A", title="R&amp;D Engineer"),
    ]))
    jobs = JazzHRScraper("acme").fetch()
    assert jobs[0].title == "R&D Engineer"


def test_decodes_entities_in_location(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=_listing([
        _row(job_id="A", location="Saint-Étienne, France"),
    ]))
    jobs = JazzHRScraper("acme").fetch()
    assert jobs[0].location == "Saint-Étienne, France"


def test_collapses_whitespace_in_location(httpx_mock) -> None:
    """Real JazzHR listings have leading/trailing whitespace inside <td>
    (from indented HTML). Output must be a single clean line."""
    row = (
        '<tr id="row_job_A" class="resumator_even_row">'
        '<td><a class="job_title_link" href="/apply/jobs/details/A?&">X</a></td>'
        '<td>\n\t\t\t\t\t\tNew York,    NY\t\t\t\t\t</td>'
        '</tr>'
    )
    httpx_mock.add_response(url=URL, text=_listing([row]))
    jobs = JazzHRScraper("acme").fetch()
    assert jobs[0].location == "New York, NY"


# --- URL composition --------------------------------------------------------


def test_builds_canonical_job_url(httpx_mock) -> None:
    """The `?&` query string suffix on JazzHR detail links is ugly noise —
    the canonical URL we emit drops it."""
    httpx_mock.add_response(url=URL, text=_listing([_row(job_id="K9zZ")]))
    jobs = JazzHRScraper("acme").fetch()
    assert str(jobs[0].url) == "https://acme.applytojob.com/apply/jobs/details/K9zZ"


# --- Error handling & retries ----------------------------------------------


def test_raises_company_not_found_on_404(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        JazzHRScraper("acme").fetch()


def test_404_does_not_retry(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.jazzhr as jh
    monkeypatch.setattr(jh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        JazzHRScraper("acme").fetch()


def test_retries_on_5xx_then_succeeds(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.jazzhr as jh
    monkeypatch.setattr(jh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, text=_listing([_row(job_id="A1")]))
    jobs = JazzHRScraper("acme").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.jazzhr as jh
    monkeypatch.setattr(jh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        JazzHRScraper("acme").fetch()


def test_429_with_retry_after_is_honored(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.jazzhr as jh
    monkeypatch.setattr(jh, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=URL, status_code=429, headers={"Retry-After": "8"}
    )
    httpx_mock.add_response(url=URL, text=_listing([_row(job_id="A1")]))
    JazzHRScraper("acme").fetch()
    assert 8.0 in sleeps


def test_network_error_raises(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.jazzhr as jh
    monkeypatch.setattr(jh, "MAX_RETRIES", 2)
    httpx_mock.add_exception(
        httpx.ConnectError("DNS failed"), url=URL, is_reusable=True
    )
    with pytest.raises(ScraperError, match="DNS failed"):
        JazzHRScraper("acme").fetch()


# --- Cloudflare / WAF fallback ---------------------------------------------


def test_client_kind_httpx_raises_on_403(httpx_mock) -> None:
    """If the user pins to httpx, surface 403 — don't silently fall back."""
    httpx_mock.add_response(url=URL, status_code=403)
    with pytest.raises(ScraperError, match="WAF"):
        JazzHRScraper("acme", client_kind="httpx").fetch()


def test_auto_falls_back_to_httpcloak_on_403(monkeypatch, httpx_mock) -> None:
    """Default `auto` mode: 403 from httpx triggers httpcloak. Stub
    httpcloak with a fake module that returns the listing successfully."""
    httpx_mock.add_response(url=URL, status_code=403)

    fake_httpcloak = SimpleNamespace(
        get=lambda url, headers, timeout: SimpleNamespace(
            status_code=200,
            text=_listing([_row(job_id="HC-1", title="Via httpcloak")]),
        )
    )
    monkeypatch.setitem(sys.modules, "httpcloak", fake_httpcloak)

    jobs = JazzHRScraper("acme").fetch()  # auto by default
    assert len(jobs) == 1
    assert jobs[0].ats_id == "HC-1"
    assert jobs[0].title == "Via httpcloak"


def test_client_kind_httpcloak_skips_httpx(monkeypatch) -> None:
    """When pinned to httpcloak, httpx must NOT be touched at all."""
    import jobhive.scrapers.jazzhr as jh

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("httpx must not be called when client_kind=httpcloak")
    monkeypatch.setattr(jh.httpx, "AsyncClient", boom)

    fake_httpcloak = SimpleNamespace(
        get=lambda url, headers, timeout: SimpleNamespace(
            status_code=200,
            text=_listing([_row(job_id="HC-2", title="Direct")]),
        )
    )
    monkeypatch.setitem(sys.modules, "httpcloak", fake_httpcloak)

    jobs = JazzHRScraper("acme", client_kind="httpcloak").fetch()
    assert [j.ats_id for j in jobs] == ["HC-2"]


def test_httpcloak_404_raises_company_not_found(monkeypatch) -> None:
    fake_httpcloak = SimpleNamespace(
        get=lambda url, headers, timeout: SimpleNamespace(
            status_code=404, text=""
        )
    )
    monkeypatch.setitem(sys.modules, "httpcloak", fake_httpcloak)
    with pytest.raises(CompanyNotFoundError):
        JazzHRScraper("acme", client_kind="httpcloak").fetch()


def test_httpcloak_module_missing_raises_helpful_error(monkeypatch) -> None:
    """If httpcloak isn't installed, the error must point to the install
    command — not just `ModuleNotFoundError`."""
    monkeypatch.setitem(sys.modules, "httpcloak", None)
    with pytest.raises(ScraperError, match="httpcloak"):
        JazzHRScraper("acme", client_kind="httpcloak").fetch()
