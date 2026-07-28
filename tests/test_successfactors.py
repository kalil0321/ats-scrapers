"""Tests for the SAP SuccessFactors scraper.

The scraper fetches public RSS 2.0 and legacy Recruiting Management XML feeds.
These tests pin URL resolution, XML parsing, field extraction, and retry.
"""

from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, SuccessFactorsScraper

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py — the shared fetch layer replaced per-scraper retry constants.

FEED_URL = "https://job.acme.com/sitemal.xml"
LEGACY_CAREER_URL = "https://career8.successfactors.com/career?company=amkor"
LEGACY_FEED_URL = (
    "https://career8.successfactors.com/career"
    "?company=amkor&career_ns=job_listing_summary&resultType=XML"
)


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


def _legacy_feed(items: list[str]) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Job-Listing>
{''.join(items)}
</Job-Listing>"""


def _legacy_item(
    *,
    title: str = "Account Manager",
    requisition_id: str = "29023",
    description: str = "<![CDATA[<p>Manage customer accounts.</p>]]>",
    filters: str = "",
    posted_date: str = "",
) -> str:
    return f"""<Job>
<JobTitle>{title}</JobTitle>
<Job-Description>{description}</Job-Description>
<ReqId>{requisition_id}</ReqId>
<Posted-Date>{posted_date}</Posted-Date>
{filters}
</Job>"""


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


def test_legacy_url_resolves_xml_feed() -> None:
    scraper = SuccessFactorsScraper(LEGACY_CAREER_URL)
    assert scraper._resolve_feed_target() == (LEGACY_FEED_URL, "amkor")


def test_legacy_url_preserves_locale() -> None:
    scraper = SuccessFactorsScraper(
        f"{LEGACY_CAREER_URL}&rcm_site_locale=de_DE&career_ns=job_listing"
    )
    assert scraper._resolve_feed_target() == (
        f"{LEGACY_FEED_URL}&rcm_site_locale=de_DE",
        "amkor",
    )


def test_china_legacy_host_resolves_xml_feed() -> None:
    scraper = SuccessFactorsScraper(
        "https://career15.sapsf.cn/career?company=volkswag09"
    )
    assert scraper._resolve_feed_target() == (
        "https://career15.sapsf.cn/career"
        "?company=volkswag09&career_ns=job_listing_summary&resultType=XML",
        "volkswag09",
    )


def test_legacy_url_discards_userinfo_and_port() -> None:
    scraper = SuccessFactorsScraper(
        "https://ignored@career8.successfactors.com:8443/career?company=amkor"
    )
    assert scraper._resolve_feed_target() == (LEGACY_FEED_URL, "amkor")


def test_successfactors_catalog_urls_are_unique() -> None:
    catalog = Path("ats-companies/successfactors.csv")
    with catalog.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    urls = [row["url"].rstrip("/") for row in rows]
    assert len(urls) == len(set(urls))


def test_legacy_catalog_urls_use_production_hosts() -> None:
    catalog = Path("ats-companies/successfactors.csv")
    with catalog.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    legacy_rows = [
        row
        for row in rows
        if (urlparse(row["url"]).hostname or "").endswith(
            (
                ".successfactors.com",
                ".successfactors.eu",
                ".sapsf.com",
                ".sapsf.eu",
                ".sapsf.cn",
            )
        )
    ]
    assert legacy_rows
    for row in legacy_rows:
        parsed = urlparse(row["url"])
        assert parsed.scheme == "https"
        assert parsed.path == "/career"
        assert len(parse_qs(parsed.query).get("company", [])) == 1
        assert not any(
            marker in (parsed.hostname or "")
            for marker in ("preview", "salesdemo", "stage")
        )


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


def test_parses_legacy_xml_feed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([
            _legacy_item(
                title="R&amp;D Manager",
                posted_date="07/26/2026",
                description=(
                    "<![CDATA[<p>Manage [[id]] in [[filter2]] "
                    "for [[missing]].</p>]]>"
                ),
                filters="<filter2>Paris</filter2>",
            )
        ]),
    )
    jobs = SuccessFactorsScraper(
        LEGACY_CAREER_URL,
        company_name="Amkor Technology",
    ).fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "amkor:29023"
    assert job.requisition_id == "29023"
    assert job.title == "R&D Manager"
    assert job.company == "Amkor Technology"
    assert job.description == "Manage 29023 in Paris for ."
    assert job.posted_at is not None
    assert job.posted_at.isoformat() == "2026-07-26T00:00:00+00:00"
    assert str(job.url) == (
        "https://career8.successfactors.com/sfcareer/jobreqcareer"
        "?jobId=29023&company=amkor"
    )


def test_legacy_feed_uses_company_id_as_fallback_name(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([_legacy_item()]),
    )
    jobs = SuccessFactorsScraper(LEGACY_CAREER_URL).fetch()
    assert jobs[0].company == "amkor"


def test_legacy_feed_dedupes_and_skips_incomplete_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([
            _legacy_item(),
            _legacy_item(title="Duplicate"),
            "<Job><JobTitle>No requisition</JobTitle></Job>",
            "<Job><ReqId>123</ReqId></Job>",
        ]),
    )
    jobs = SuccessFactorsScraper(LEGACY_CAREER_URL).fetch()
    assert [job.title for job in jobs] == ["Account Manager"]


def test_legacy_feed_can_omit_descriptions(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([_legacy_item()]),
    )
    jobs = SuccessFactorsScraper(
        LEGACY_CAREER_URL,
        include_descriptions=False,
    ).fetch()
    assert jobs[0].description is None


def test_legacy_feed_ignores_invalid_posted_date(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([_legacy_item(posted_date="not-a-date")]),
    )
    jobs = SuccessFactorsScraper(LEGACY_CAREER_URL).fetch()
    assert jobs[0].posted_at is None


def test_legacy_feed_infers_day_first_dates(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([
            _legacy_item(
                requisition_id="day-first-signal",
                posted_date="27/07/2026",
            ),
            _legacy_item(
                requisition_id="ambiguous",
                posted_date="08/07/2026",
            ),
        ]),
    )
    jobs = SuccessFactorsScraper(LEGACY_CAREER_URL).fetch()
    assert [job.posted_at.isoformat() for job in jobs if job.posted_at] == [
        "2026-07-27T00:00:00+00:00",
        "2026-07-08T00:00:00+00:00",
    ]


def test_legacy_feed_keeps_month_first_dates(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([
            _legacy_item(
                requisition_id="month-first-signal",
                posted_date="07/27/2026",
            ),
            _legacy_item(
                requisition_id="ambiguous",
                posted_date="07/08/2026",
            ),
        ]),
    )
    jobs = SuccessFactorsScraper(LEGACY_CAREER_URL).fetch()
    assert [job.posted_at.isoformat() for job in jobs if job.posted_at] == [
        "2026-07-27T00:00:00+00:00",
        "2026-07-08T00:00:00+00:00",
    ]


def test_legacy_feed_removes_invalid_empty_name_elements(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LEGACY_FEED_URL,
        text=_legacy_feed([
            _legacy_item().replace("</Job>", "<>250</></Job>")
        ]),
    )
    jobs = SuccessFactorsScraper(LEGACY_CAREER_URL).fetch()
    assert [job.ats_id for job in jobs] == ["amkor:29023"]


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


def test_5xx_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, status_code=503)
    httpx_mock.add_response(url=FEED_URL, text=_rss([_item()]))
    jobs = SuccessFactorsScraper("job.acme.com").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch
    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 2)
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


def test_non_job_listing_legacy_xml_raises_clean_error(httpx_mock) -> None:
    httpx_mock.add_response(url=LEGACY_FEED_URL, text="<rss></rss>")
    with pytest.raises(ScraperError, match="non-job-listing"):
        SuccessFactorsScraper(LEGACY_CAREER_URL).fetch()
