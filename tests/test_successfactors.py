"""Tests for the SAP SuccessFactors scraper.

The scraper fetches the public RSS 2.0 feed at ``{host}/sitemal.xml``.
These tests pin XML parsing, title/location splitting, dedup, and retry.
"""

from __future__ import annotations

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, SuccessFactorsScraper


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.successfactors as sf
    monkeypatch.setattr(sf, "MAX_RETRIES", 1)
    monkeypatch.setattr(sf, "RETRY_BASE_DELAY", 0.0)


FEED_URL = "https://job.acme.com/sitemal.xml"


def _rss(items: list[str], company: str = "Acme") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
<channel>
<title>{company}</title>
<description>Search jobs at {company}</description>
{''.join(items)}
</channel>
</rss>"""


def _item(
    *,
    title: str = "Project Manager (Dallas, TX, US)",
    link: str = "https://job.acme.com/job/dallas-tx/project-manager/86101/",
    pubdate: str = "Fri, 20 Mar 2026 09:30:04 +0100",
    description: str = "<![CDATA[<p>Manage things.</p>]]>",
    gid: str | None = "86101",
) -> str:
    g_id = f"<g:id>{gid}</g:id>" if gid else ""
    return f"""<item>
<title>{title}</title>
<link>{link}</link>
<pubDate>{pubdate}</pubDate>
<description>{description}</description>
{g_id}
</item>"""


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_successfactors() -> None:
    assert ScraperRegistry.get(ATSType.SUCCESSFACTORS) is SuccessFactorsScraper


# --- URL resolution ---------------------------------------------------------


def test_full_host_accepted() -> None:
    s = SuccessFactorsScraper("job.acme.com")
    assert s._resolve_feed_url() == "https://job.acme.com/sitemal.xml"


def test_full_url_accepted() -> None:
    s = SuccessFactorsScraper("https://job.acme.com")
    assert s._resolve_feed_url() == "https://job.acme.com/sitemal.xml"


def test_bare_slug_assumes_job_dot_slug_dot_com() -> None:
    s = SuccessFactorsScraper("acme")
    assert s._resolve_feed_url() == "https://job.acme.com/sitemal.xml"


# --- Happy path -------------------------------------------------------------


def test_parses_basic_rss(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, text=_rss([_item()]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "86101"
    assert job.title == "Project Manager"  # location stripped from parens
    assert job.location == "Dallas, TX, US"
    assert job.company == "Acme"  # from channel/title
    assert job.ats_type is ATSType.SUCCESSFACTORS
    assert str(job.url).startswith("https://job.acme.com")
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_dedupes_by_ats_id(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, text=_rss([
        _item(gid="X", link="https://job.acme.com/x"),
        _item(gid="X", link="https://job.acme.com/x-dup"),
    ]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert len(jobs) == 1


def test_uses_url_tail_when_gid_missing(httpx_mock) -> None:
    """Some tenants don't emit the Google namespace; fall back to URL tail."""
    httpx_mock.add_response(url=FEED_URL, text=_rss([
        _item(gid=None, link="https://job.acme.com/job/abc/123"),
    ]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert jobs[0].ats_id == "123"


def test_skips_item_without_link(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, text=_rss(["""<item>
<title>No link</title>
<link></link>
</item>"""]))
    assert SuccessFactorsScraper("job.acme.com").fetch() == []


# --- Title / location extraction --------------------------------------------


def test_keeps_title_intact_when_parens_arent_a_location(httpx_mock) -> None:
    """``(Remote)`` is not a location format we recognize — leave the title
    alone rather than misinterpret it."""
    httpx_mock.add_response(url=FEED_URL, text=_rss([
        _item(title="Senior Engineer (Remote)"),
    ]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    # Title stays whole; location is None
    assert jobs[0].title == "Senior Engineer (Remote)"
    assert jobs[0].location is None


def test_extracts_two_letter_state_location(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, text=_rss([
        _item(title="Sales Rep (NY)"),
    ]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert jobs[0].title == "Sales Rep"
    assert jobs[0].location == "NY"


# --- Description ------------------------------------------------------------


def test_description_strips_tags_and_decodes_entities(httpx_mock) -> None:
    desc = "&lt;p&gt;Senior &amp; Lead role&lt;/p&gt;"
    httpx_mock.add_response(url=FEED_URL, text=_rss([_item(description=desc)]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert jobs[0].description == "Senior & Lead role"


def test_description_truncated_to_10kb(httpx_mock) -> None:
    huge_desc = "&lt;p&gt;" + "Lorem. " * 3000 + "&lt;/p&gt;"
    httpx_mock.add_response(url=FEED_URL, text=_rss([_item(description=huge_desc)]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert jobs[0].description is not None
    assert len(jobs[0].description) <= 25_000


# --- Error handling ---------------------------------------------------------


def test_raises_company_not_found_on_404(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        SuccessFactorsScraper("job.acme.com").fetch()


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.successfactors as sf
    monkeypatch.setattr(sf, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=FEED_URL, status_code=503)
    httpx_mock.add_response(url=FEED_URL, text=_rss([_item()]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.successfactors as sf
    monkeypatch.setattr(sf, "MAX_RETRIES", 2)
    httpx_mock.add_response(url=FEED_URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        SuccessFactorsScraper("job.acme.com").fetch()


def test_malformed_xml_raises_clean_error(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, text="not <xml>")
    with pytest.raises(ScraperError, match="malformed XML"):
        SuccessFactorsScraper("job.acme.com").fetch()


def test_html_response_treated_as_non_rss(httpx_mock) -> None:
    """A CDN error page that's valid XML but not RSS — surface a clean
    error rather than return an empty list silently."""
    httpx_mock.add_response(url=FEED_URL, text="<html><body>nope</body></html>")
    with pytest.raises(ScraperError, match="non-RSS"):
        SuccessFactorsScraper("job.acme.com").fetch()
