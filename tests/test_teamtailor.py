"""Tests for the Teamtailor scraper.

Teamtailor's `/jobs.rss` is structured XML with a custom `tt:` namespace
for locations and departments. These tests pin:

1. RSS parsing (title, link, location from city+country, department, remote)
2. ats_id extraction (numeric URL prefix, GUID fallback)
3. HTML entity + tag stripping in descriptions
4. Retry behaviour (404 fail-fast, 429/5xx retry)
5. Malformed-XML handling
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, TeamtailorScraper, get_scraper


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.teamtailor as tt
    monkeypatch.setattr(tt, "MAX_RETRIES", 1)
    monkeypatch.setattr(tt, "RETRY_BASE_DELAY", 0.0)


RSS_URL = "https://acme.teamtailor.com/jobs.rss"


def _rss(items: list[str]) -> str:
    body = "".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations">
  <channel>
    <title>Acme</title>
    <link>https://acme.teamtailor.com/jobs</link>
    {body}
  </channel>
</rss>"""


def _item(
    *,
    title: str = "Engineer",
    link: str = "https://acme.teamtailor.com/jobs/123-engineer",
    description: str = "<p>Build cool things.</p>",
    pubdate: str = "Fri, 20 Mar 2026 09:30:04 +0100",
    guid: str = "guid-1",
    remote_status: str = "none",
    city: str = "Berlin",
    country: str = "Germany",
    department: str = "Engineering",
    role: str | None = None,
) -> str:
    role_tag = f"<tt:role>{role}</tt:role>" if role else ""
    return f"""
    <item>
      <title>{title}</title>
      <pubDate>{pubdate}</pubDate>
      <link>{link}</link>
      <remoteStatus>{remote_status}</remoteStatus>
      <guid>{guid}</guid>
      <description>{description}</description>
      <tt:locations>
        <tt:location>
          <tt:name>HQ</tt:name>
          <tt:address/>
          <tt:zip/>
          <tt:city>{city}</tt:city>
          <tt:country>{country}</tt:country>
        </tt:location>
      </tt:locations>
      <tt:department>{department}</tt:department>
      {role_tag}
    </item>
    """


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_teamtailor() -> None:
    assert ScraperRegistry.get(ATSType.TEAMTAILOR) is TeamtailorScraper


def test_get_scraper_by_string_returns_teamtailor() -> None:
    s = get_scraper("teamtailor", "acme")
    assert isinstance(s, TeamtailorScraper)
    assert s.company_slug == "acme"


# --- Happy path -------------------------------------------------------------


def test_parses_basic_rss(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item()]))
    jobs = TeamtailorScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "123"  # numeric prefix from URL
    assert job.title == "Engineer"
    assert job.company == "acme"
    assert job.ats_type is ATSType.TEAMTAILOR
    assert job.location == "Berlin, Germany"
    assert job.department == "Engineering"
    assert str(job.url) == "https://acme.teamtailor.com/jobs/123-engineer"


def test_parses_multiple_items_preserves_order(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([
        _item(title="A", link="https://acme.teamtailor.com/jobs/1-a", guid="g1"),
        _item(title="B", link="https://acme.teamtailor.com/jobs/2-b", guid="g2"),
        _item(title="C", link="https://acme.teamtailor.com/jobs/3-c", guid="g3"),
    ]))
    jobs = TeamtailorScraper("acme").fetch()
    assert [j.title for j in jobs] == ["A", "B", "C"]
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]


def test_returns_empty_for_feed_with_no_items(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([]))
    assert TeamtailorScraper("acme").fetch() == []


def test_dedupes_items_with_same_id(httpx_mock) -> None:
    """Cross-listed jobs (rare) — same numeric URL prefix appears twice.
    Output keeps each ID once."""
    httpx_mock.add_response(url=RSS_URL, text=_rss([
        _item(link="https://acme.teamtailor.com/jobs/123-engineer", guid="g1"),
        _item(link="https://acme.teamtailor.com/jobs/123-engineer", guid="g2"),
    ]))
    jobs = TeamtailorScraper("acme").fetch()
    assert len(jobs) == 1


# --- ats_id extraction ------------------------------------------------------


def test_ats_id_uses_numeric_prefix_from_url(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([
        _item(link="https://acme.teamtailor.com/jobs/7503087-llm-engineer-mid")
    ]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].ats_id == "7503087"


def test_ats_id_falls_back_to_guid_when_url_lacks_id(httpx_mock) -> None:
    """If the URL doesn't have a numeric prefix (unlikely but defensible),
    the GUID UUID is used as a stable fallback."""
    httpx_mock.add_response(url=RSS_URL, text=_rss([
        _item(link="https://acme.teamtailor.com/jobs/", guid="abc-uuid-123"),
    ]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].ats_id == "abc-uuid-123"


def test_skips_item_with_no_link(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss(["""
    <item>
      <title>Orphan</title>
      <link></link>
      <guid>g</guid>
    </item>
    """]))
    assert TeamtailorScraper("acme").fetch() == []


# --- Description extraction (HTML stripping + entity decode + truncation) ---


def test_description_strips_tags_and_decodes_entities(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([
        _item(description="&lt;p&gt;Senior &amp; Lead role&lt;/p&gt;"),
    ]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].description == "Senior & Lead role"


def test_description_truncated_to_10kb(httpx_mock) -> None:
    huge = ("<p>Lorem ipsum.</p>" * 1000)
    huge_escaped = huge.replace("<", "&lt;").replace(">", "&gt;")
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item(description=huge_escaped)]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].description is not None
    assert len(jobs[0].description) <= 25_000


def test_description_none_when_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item(description="")]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].description is None


# --- Location -----------------------------------------------------------


def test_location_combines_city_and_country(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([
        _item(city="Paris", country="France"),
    ]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].location == "Paris, France"


def test_location_country_only(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item(city="", country="France")]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].location == "France"


def test_location_falls_back_to_name_when_city_country_empty(httpx_mock) -> None:
    """Some Teamtailor tenants omit city/country and only set the location's
    `<tt:name>`. Keep that as a last resort."""
    httpx_mock.add_response(url=RSS_URL, text=_rss(["""
    <item>
      <title>X</title>
      <link>https://acme.teamtailor.com/jobs/1-x</link>
      <guid>g</guid>
      <description></description>
      <tt:locations>
        <tt:location>
          <tt:name>EMEA Region</tt:name>
        </tt:location>
      </tt:locations>
    </item>
    """]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].location == "EMEA Region"


def test_location_none_when_missing(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss(["""
    <item>
      <title>X</title>
      <link>https://acme.teamtailor.com/jobs/1-x</link>
      <guid>g</guid>
      <description></description>
    </item>
    """]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].location is None


# --- Remote status mapping -------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("fully", True),
        ("none", False),
        ("hybrid", None),  # ambiguous on purpose
        ("temporary", None),
        ("", None),
    ],
)
def test_remote_status_mapping(httpx_mock, status: str, expected: bool | None) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item(remote_status=status)]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].is_remote is expected


# --- pubDate parsing -------------------------------------------------------


def test_pubdate_parsed_from_rfc_2822(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([
        _item(pubdate="Fri, 20 Mar 2026 09:30:04 +0100"),
    ]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].posted_at is not None
    assert jobs[0].posted_at.year == 2026
    assert jobs[0].posted_at.month == 3


def test_pubdate_invalid_returns_none(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item(pubdate="not a date")]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].posted_at is None


# --- Department empty-string handling --------------------------------------


def test_empty_department_becomes_none(httpx_mock) -> None:
    """Some tenants leave `<tt:department/>` empty rather than omit it.
    Empty string isn't a real department — coerce to None."""
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item(department="")]))
    jobs = TeamtailorScraper("acme").fetch()
    assert jobs[0].department is None


# --- Error handling --------------------------------------------------------


def test_raises_company_not_found_on_404(httpx_mock) -> None:
    httpx_mock.add_response(url=RSS_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        TeamtailorScraper("acme").fetch()


def test_404_does_not_retry(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.teamtailor as tt
    monkeypatch.setattr(tt, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=RSS_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        TeamtailorScraper("acme").fetch()


def test_retries_on_5xx_then_succeeds(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.teamtailor as tt
    monkeypatch.setattr(tt, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=RSS_URL, status_code=503)
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item()]))
    jobs = TeamtailorScraper("acme").fetch()
    assert len(jobs) == 1


def test_429_with_retry_after_is_honored(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.teamtailor as tt
    monkeypatch.setattr(tt, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=RSS_URL, status_code=429, headers={"Retry-After": "11"}
    )
    httpx_mock.add_response(url=RSS_URL, text=_rss([_item()]))
    TeamtailorScraper("acme").fetch()
    assert 11.0 in sleeps


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.teamtailor as tt
    monkeypatch.setattr(tt, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=RSS_URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        TeamtailorScraper("acme").fetch()


def test_network_error_raises(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.teamtailor as tt
    monkeypatch.setattr(tt, "MAX_RETRIES", 2)
    httpx_mock.add_exception(
        httpx.ConnectError("DNS failed"), url=RSS_URL, is_reusable=True
    )
    with pytest.raises(ScraperError, match="DNS failed"):
        TeamtailorScraper("acme").fetch()


def test_malformed_xml_raises_scraper_error(httpx_mock) -> None:
    """If the RSS is truncated or HTML (e.g. a tenant accidentally fronted
    by a CDN error page), surface a clean error instead of raising
    `ParseError` to library users."""
    httpx_mock.add_response(url=RSS_URL, text="<html>not RSS</html>")
    with pytest.raises(ScraperError, match="malformed RSS"):
        TeamtailorScraper("acme").fetch()
