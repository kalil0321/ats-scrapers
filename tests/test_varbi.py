from __future__ import annotations

from html import escape

import pytest

from ats_scrapers import ATSType, resolve_careers_url
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.scrapers import VarbiScraper
from ats_scrapers.scrapers.base import ScraperRegistry

FEED = "https://acme.varbi.com/what:rssfeed/"
DETAIL = "https://acme.varbi.com/en/what:job/jobID:123/"
DESCRIPTION = "Build reliable data pipelines for scientific research."


def _item(url: str = DETAIL, title: str = "Research Engineer", description: str = DESCRIPTION) -> str:
    return (
        f"<item><title>{escape(title)}</title><link>{escape(url)}</link>"
        f"<description>{escape(description)}</description>"
        "<pubDate>Fri, 11 Sep 2026 00:00:00 +0200</pubDate></item>"
    )


def _feed(items: str | None = None, company: str = "New jobs at Acme University") -> str:
    return (
        f"<rss><channel><title>{escape(company)}</title>"
        f"{_item() if items is None else items}</channel></rss>"
    )


def _detail(title: str = "Research Engineer", deadline: str = "2099-10-11") -> str:
    fields = {
        "town": "Stockholm", "county": "Stockholms län", "country": "Sweden",
        "type-of-employment": "Permanent position", "hours": "Full time",
        "reference-number": "REF-123", "ends": deadline,
    }
    rows = "".join(
        f'<tr class="quick-info-{field}"><td>{escape(value)}</td></tr>'
        for field, value in fields.items()
    )
    return (
        f'<html><meta property="og:url" content="{DETAIL}">'
        f'<h1>{escape(title)}</h1><table class="quick-info">{rows}</table>'
        '<a href="https://acme.varbi.com/en/what:login/jobID:123/type:job/apply:1/">Apply</a>'
        "</html>"
    )


def test_registry_and_url_resolution() -> None:
    assert ScraperRegistry.get(ATSType.VARBI) is VarbiScraper
    result = resolve_careers_url(DETAIL)
    assert result is not None
    assert result.ats is ATSType.VARBI
    assert result.slug == "acme"
    assert resolve_careers_url("https://www.varbi.com/") is None
    assert resolve_careers_url("https://nested.acme.varbi.com/") is None


def test_fetches_complete_jobs_without_credentials(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail())
    jobs = VarbiScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.VARBI
    assert job.ats_id == "acme:123"
    assert job.company == "Acme University"
    assert job.description == DESCRIPTION
    assert job.location == "Stockholm, Stockholms län, Sweden"
    assert job.country_iso == "SE"
    assert job.region == "Europe"
    assert job.requisition_id == "REF-123"
    assert job.employment_type == "FULL_TIME"
    assert job.raw["application_deadline_date"] == "2099-10-11"
    assert job.application_deadline is None
    assert job.posted_at.isoformat() == "2026-09-10T22:00:00+00:00"
    assert str(job.apply_url).endswith("jobID:123/type:job/apply:1/")
    job.model_dump(mode="json", warnings="error")


def test_listing_only_and_description_lookup(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed(), is_reusable=True)
    scraper = VarbiScraper("acme", include_descriptions=False)
    job = scraper.fetch()[0]
    assert job.description is None
    assert scraper.get_description(job) == DESCRIPTION
    assert len(httpx_mock.get_requests()) == 2


def test_catalog_name_override_and_swedish_feed() -> None:
    xml = _feed(company="Nya lediga jobb hos Acme universitet")
    assert VarbiScraper("acme")._parse_feed(xml)[0].company == "Acme universitet"
    assert VarbiScraper("acme", company_name="Acme University")._parse_feed(xml)[0].company == "Acme University"


def test_duplicate_locales_share_one_identity(httpx_mock) -> None:
    xml = _feed(_item() + _item(DETAIL.replace("/en/", "/se/")))
    httpx_mock.add_response(url=FEED, text=xml)
    httpx_mock.add_response(url=DETAIL, text=_detail())
    assert len(VarbiScraper("acme").fetch()) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_removed_details_are_excluded(httpx_mock, status) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, status_code=status)
    assert VarbiScraper("acme").fetch() == []


@pytest.mark.parametrize("deadline", [
    "2000-01-01", "01.Jan.2000", "01.okt.2000", "01-10-2000", "01.10.2000",
    "1 January 2000", "01 januari 2000", "1.October.2000", "1 oktober 2000",
])
def test_expired_dates_are_excluded(httpx_mock, deadline) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail(deadline=deadline))
    assert VarbiScraper("acme").fetch() == []


def test_unknown_date_is_not_replaced_with_current_time(httpx_mock) -> None:
    xml = _feed().replace("Fri, 11 Sep 2026 00:00:00 +0200", "unknown")
    httpx_mock.add_response(url=FEED, text=xml)
    job = VarbiScraper("acme", include_descriptions=False).fetch()[0]
    assert job.posted_at is None


def test_dutch_metadata_preserves_date_and_country(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed(company="Nieuwe vacatures bij Acme"))
    detail = _detail(deadline="27-09-2099").replace("Sweden", "Nederland").replace("Full time", "Fulltime")
    httpx_mock.add_response(url=DETAIL, text=detail)
    job = VarbiScraper("acme").fetch()[0]
    assert job.country_iso == "NL"
    assert job.employment_type == "FULL_TIME"
    assert job.raw["application_deadline_date"] == "2099-09-27"


def test_empty_recognized_feed(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed(""))
    assert VarbiScraper("acme").fetch() == []


@pytest.mark.parametrize("title", [
    "Postdoctoral studies in Immunology (scholarship)",
    "Intresseanmälan till tentavakt", "Spontanansökan", "General application",
    "Open sollicitatie", "Spontane sollicitatie", "Uopfordret ansøgning",
    "Spontanansøgning",
])
def test_non_vacancy_items_are_not_jobs(httpx_mock, title) -> None:
    httpx_mock.add_response(url=FEED, text=_feed(_item(title=title)))
    assert VarbiScraper("acme").fetch() == []


def test_scholarship_administrator_is_a_real_vacancy() -> None:
    jobs = VarbiScraper("acme")._parse_feed(_feed(_item(title="Scholarship administrator")))
    assert len(jobs) == 1


def test_mixed_hours_are_not_misclassified(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail().replace("Full time", "Fulltime/Parttime"))
    assert VarbiScraper("acme").fetch()[0].employment_type is None


@pytest.mark.parametrize("xml", ["<html>Maintenance</html>", "<rss>", '<!DOCTYPE rss [<!ENTITY name "Acme">]><rss/>'])
def test_malformed_and_unrecognized_feeds_fail_closed(httpx_mock, xml) -> None:
    httpx_mock.add_response(url=FEED, text=xml)
    with pytest.raises(ScraperError):
        VarbiScraper("acme").fetch()


@pytest.mark.parametrize("url", [
    "https://other.varbi.com/en/what:job/jobID:123/",
    "https://acme.varbi.com.evil.example/en/what:job/jobID:123/",
    "https://acme.varbi.com:8443/en/what:job/jobID:123/",
    "http://acme.varbi.com/en/what:job/jobID:123/",
    "https://acme.varbi.com/en/what:login/jobID:123/",
])
def test_untrusted_feed_links_fail_closed(httpx_mock, url) -> None:
    httpx_mock.add_response(url=FEED, text=_feed(_item(url)))
    with pytest.raises(ScraperError, match="untrusted job URL"):
        VarbiScraper("acme").fetch()
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("target", [FEED, DETAIL])
def test_redirect_does_not_follow_untrusted_destinations(httpx_mock, target) -> None:
    if target == DETAIL:
        httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=target, status_code=302, headers={"Location": "https://example.org/login"})
    with pytest.raises(ScraperError, match="302"):
        VarbiScraper("acme").fetch()
    assert [str(request.url) for request in httpx_mock.get_requests()] == (
        [FEED, DETAIL] if target == DETAIL else [FEED]
    )


def test_mismatched_detail_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail().replace('jobID:123/', 'jobID:999/'))
    with pytest.raises(ScraperError, match="did not match"):
        VarbiScraper("acme").fetch()


def test_translated_detail_keeps_consistent_feed_title_and_description(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail(title="Forskningsingenjör"))
    job = VarbiScraper("acme").fetch()[0]
    assert job.title == "Research Engineer"
    assert job.description == DESCRIPTION


@pytest.mark.parametrize("detail", [
    _detail().replace(f'<meta property="og:url" content="{DETAIL}">', ''),
    _detail().replace(DETAIL, DETAIL.replace("acme.varbi.com", "other.varbi.com")),
    _detail().replace('class="quick-info"', 'class="unrelated"'),
])
def test_unverified_detail_identity_fails_closed(httpx_mock, detail) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=detail)
    with pytest.raises(ScraperError, match="did not match"):
        VarbiScraper("acme").fetch()


@pytest.mark.parametrize("items", [_item(title=""), _item(description="")])
def test_incomplete_feed_rows_fail_closed(items) -> None:
    with pytest.raises(ScraperError, match="omitted"):
        VarbiScraper("acme")._parse_feed(_feed(items))


def test_unknown_employer_fails_closed() -> None:
    with pytest.raises(ScraperError, match="employer name"):
        VarbiScraper("acme")._parse_feed(_feed(company="Jobs"))


@pytest.mark.parametrize("slug", ["www", "api", "support", "login", "../acme", "acme.varbi.com"])
def test_rejects_non_tenant_input(slug) -> None:
    with pytest.raises((ScraperError, ValueError)):
        VarbiScraper(slug)


@pytest.mark.parametrize("slug", ["www", "api", "support", "login"])
def test_resolver_rejects_platform_subdomains(slug) -> None:
    assert resolve_careers_url(f"https://{slug}.varbi.com/") is None


@pytest.mark.parametrize("description", [
    "<p>Build <strong>reliable</strong> pipelines.</p>",
    "&lt;p&gt;Build &lt;strong&gt;reliable&lt;/strong&gt; pipelines.&lt;/p&gt;",
])
def test_feed_description_is_plain_text(description) -> None:
    jobs = VarbiScraper("acme")._parse_feed(_feed(_item(description=description)))
    assert jobs[0].description == "Build reliable pipelines."


@pytest.mark.parametrize("deadline", ["not a date", "31 February 2099"])
def test_unrecognized_deadline_fails_closed(httpx_mock, deadline) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail(deadline=deadline))
    with pytest.raises(ScraperError, match="unrecognized deadline"):
        VarbiScraper("acme").fetch()


def test_missing_location_is_optional(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    detail = _detail()
    for field in ("town", "county", "country"):
        detail = detail.replace(f'quick-info-{field}', f'unrelated-{field}')
    httpx_mock.add_response(url=DETAIL, text=detail)
    assert VarbiScraper("acme").fetch()[0].location is None


def test_uppercase_tenant_uses_canonical_host(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail())
    job = VarbiScraper("AcMe").fetch()[0]
    assert job.ats_id == "acme:123"
    assert job.apply_url is not None


@pytest.mark.parametrize("contract", ["Contract", "Contractor", " CONTRACT "])
def test_contract_employment_type(httpx_mock, contract) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    httpx_mock.add_response(url=DETAIL, text=_detail().replace("Permanent position", contract))
    assert VarbiScraper("acme").fetch()[0].employment_type == "CONTRACT"


@pytest.mark.parametrize(("job_id", "accepted"), [("123", True), ("999", False)])
def test_quick_apply_links_must_match_the_listed_job(httpx_mock, job_id, accepted) -> None:
    httpx_mock.add_response(url=FEED, text=_feed())
    quick_path = f"en/apply/positionquick/{job_id}/"
    detail = _detail().replace("en/what:login/jobID:123/type:job/apply:1/", quick_path)
    httpx_mock.add_response(url=DETAIL, text=detail)
    job = VarbiScraper("acme").fetch()[0]
    if accepted:
        assert str(job.apply_url) == f"https://acme.varbi.com/{quick_path}"
    else:
        assert job.apply_url is None
