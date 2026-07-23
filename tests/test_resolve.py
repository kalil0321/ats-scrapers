"""Tests for careers-URL resolution (`ats_scrapers.resolve`)."""

from __future__ import annotations

import pytest

from ats_scrapers import get_scraper_for_url, resolve_careers_url
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import (
    AshbyScraper,
    BeisenLegacyScraper,
    BeisenScraper,
    DarwinboxScraper,
    GreenhouseScraper,
    GupyScraper,
    MokaScraper,
    WorkdayScraper,
)

RESOLVES = [
    # Path-based tenants
    ("https://jobs.ashbyhq.com/openai", ATSType.ASHBY, "openai"),
    ("https://jobs.ashbyhq.com/openai/some-posting-id", ATSType.ASHBY, "openai"),
    ("https://boards.greenhouse.io/anthropic", ATSType.GREENHOUSE, "anthropic"),
    ("https://job-boards.greenhouse.io/anthropic/jobs/123", ATSType.GREENHOUSE, "anthropic"),
    ("https://boards.eu.greenhouse.io/acme", ATSType.GREENHOUSE, "acme"),
    ("https://jobs.lever.co/palantir", ATSType.LEVER, "palantir"),
    ("https://jobs.eu.lever.co/acme", ATSType.LEVER, "acme"),
    ("https://apply.workable.com/0x/", ATSType.WORKABLE, "0x"),
    ("https://careers.smartrecruiters.com/10Pearls", ATSType.SMARTRECRUITERS, "10Pearls"),
    ("https://jobs.gem.com/accel", ATSType.GEM, "accel"),
    ("https://ats.rippling.com/acme/jobs", ATSType.RIPPLING, "acme"),
    ("https://join.com/companies/acme", ATSType.JOIN_COM, "acme"),
    (
        "https://app.mokahr.com/social-recruitment/trip/70415/job/123",
        ATSType.MOKA,
        "trip/70415",
    ),
    (
        "https://hire-r1.mokahr.com/campus-recruitment/klook/100008011",
        ATSType.MOKA,
        "hire-r1/klook/100008011/campus",
    ),
    (
        "https://airtel.darwinbox.in/ms/candidate/careers",
        ATSType.DARWINBOX,
        "airtel",
    ),
    (
        "https://pwc.darwinbox.com/ms/candidate/careers",
        ATSType.DARWINBOX,
        "pwc.com",
    ),
    ("https://mengniu.zhiye.com/social/jobs", ATSType.BEISEN, "mengniu"),
    ("https://amer.zhiye.com/Social/?PageIndex=1", ATSType.BEISEN_LEGACY, "amer"),
    ("https://amer.zhiye.com/zpdetail/123", ATSType.BEISEN_LEGACY, "amer"),
    ("https://newhope.zhiye.com/index", ATSType.BEISEN_LEGACY, "newhope"),
    # Subdomain tenants
    ("https://12build.recruitee.com", ATSType.RECRUITEE, "12build"),
    ("https://1komma5.teamtailor.com/jobs", ATSType.TEAMTAILOR, "1komma5"),
    ("https://acme.breezy.hr", ATSType.BREEZY, "acme"),
    ("https://10web.bamboohr.com/careers", ATSType.BAMBOOHR, "10web"),
    ("https://10xfounders.jobs.personio.com", ATSType.PERSONIO, "10xfounders"),
    ("https://aawdc.pinpointhq.com", ATSType.PINPOINT, "aawdc"),
    ("https://acme.applytojob.com/apply", ATSType.JAZZHR, "acme"),
    ("https://bloomberg.avature.net/careers", ATSType.AVATURE, "bloomberg"),
    ("https://nvidia.eightfold.ai/careers", ATSType.EIGHTFOLD, "nvidia"),
    ("https://petz.gupy.io/jobs/123", ATSType.GUPY, "petz"),
    # Scheme-less input is tolerated
    ("jobs.ashbyhq.com/openai", ATSType.ASHBY, "openai"),
    ("www.jobs.lever.co/acme", ATSType.LEVER, "acme"),
    # iCIMS standard portal prefix -> bare slug
    ("https://careers-peraton.icims.com/jobs/search", ATSType.ICIMS, "peraton"),
]


@pytest.mark.parametrize(("url", "ats", "slug"), RESOLVES)
def test_resolves(url: str, ats: ATSType, slug: str) -> None:
    resolved = resolve_careers_url(url)
    assert resolved is not None, f"expected {url} to resolve"
    assert resolved.ats is ats
    assert resolved.slug == slug


def test_workday_slug_is_full_url() -> None:
    resolved = resolve_careers_url(
        "https://2020companies.wd1.myworkdayjobs.com/external_careers"
    )
    assert resolved is not None
    assert resolved.ats is ATSType.WORKDAY
    assert resolved.slug == "https://2020companies.wd1.myworkdayjobs.com/external_careers"


def test_taleo_slug_keeps_query() -> None:
    url = "https://phe.tbe.taleo.net/phe01/ats/careers/v2/searchResults?org=UH9TY5&cws=41"
    resolved = resolve_careers_url(url)
    assert resolved is not None
    assert resolved.ats is ATSType.TALEO
    assert resolved.slug == url


def test_icims_nonstandard_prefix_keeps_full_url() -> None:
    resolved = resolve_careers_url("https://uscareers-rws.icims.com/jobs/search")
    assert resolved is not None
    assert resolved.ats is ATSType.ICIMS
    assert resolved.slug == "https://uscareers-rws.icims.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example.com/jobs",       # custom domain
        "https://www.linkedin.com/jobs/view/1",   # aggregator
        "https://jobs.ashbyhq.com",               # no slug in path
        "https://join.com/about",                 # join.com non-company path
        "https://evil.recruitee.com.attacker.io", # suffix-spoofing host
        "https://bad_slug.darwinbox.in/ms/candidate/careers",
        f"https://{'a' * 64}.darwinbox.com/ms/candidate/careers",
        "https://trailing-.darwinbox.in/ms/candidate/careers",
        "https://bad_slug.zhiye.com/social/jobs",
        f"https://{'a' * 64}.zhiye.com/social/jobs",
        "not a url at all",
    ],
)
def test_unrecognized_urls_return_none(url: str) -> None:
    assert resolve_careers_url(url) is None


def test_get_scraper_for_url_builds_scraper() -> None:
    scraper = get_scraper_for_url(
        "https://jobs.ashbyhq.com/openai", include_descriptions=False
    )
    assert isinstance(scraper, AshbyScraper)
    assert scraper.company_slug == "openai"
    assert scraper.include_descriptions is False

    assert isinstance(
        get_scraper_for_url("https://boards.greenhouse.io/anthropic"),
        GreenhouseScraper,
    )
    assert isinstance(get_scraper_for_url("https://petz.gupy.io"), GupyScraper)
    assert isinstance(
        get_scraper_for_url(
            "https://app.mokahr.com/social-recruitment/trip/70415"
        ),
        MokaScraper,
    )
    assert isinstance(
        get_scraper_for_url(
            "https://airtel.darwinbox.in/ms/candidate/careers"
        ),
        DarwinboxScraper,
    )
    assert isinstance(
        get_scraper_for_url("https://mengniu.zhiye.com/social/jobs"),
        BeisenScraper,
    )
    assert isinstance(
        get_scraper_for_url("https://amer.zhiye.com/Social/?PageIndex=1"),
        BeisenLegacyScraper,
    )
    workday = get_scraper_for_url(
        "https://2020companies.wd1.myworkdayjobs.com/external_careers"
    )
    assert isinstance(workday, WorkdayScraper)


def test_get_scraper_for_url_raises_with_guidance() -> None:
    with pytest.raises(ScraperError, match="Could not recognize"):
        get_scraper_for_url("https://careers.example.com/jobs")


def test_get_scraper_for_url_from_fresh_interpreter() -> None:
    """Root-level import alone must suffice — the registry has to be
    populated by the call itself, not by earlier scraper imports in
    the calling process (regression for the empty-registry bug)."""
    import subprocess
    import sys

    code = (
        "from ats_scrapers import get_scraper_for_url; "
        "s = get_scraper_for_url('https://jobs.ashbyhq.com/openai'); "
        "print(type(s).__name__, s.company_slug)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "AshbyScraper openai"
