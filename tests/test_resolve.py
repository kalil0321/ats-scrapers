"""Tests for careers-URL resolution (`ats_scrapers.resolve`)."""

from __future__ import annotations

import pytest

from ats_scrapers import get_scraper_for_url, resolve_careers_url
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import (
    AppliTrackScraper,
    AshbyScraper,
    BeisenLegacyScraper,
    BeisenScraper,
    DarwinboxScraper,
    DayforceScraper,
    GreenhouseScraper,
    GupyScraper,
    HerpScraper,
    HrmosScraper,
    JobviteScraper,
    KekaScraper,
    MokaScraper,
    NinehireScraper,
    PageUpScraper,
    PaycomScraper,
    PaylocityScraper,
    SoftgardenScraper,
    UKGProScraper,
    WinTalentScraper,
    WorkdayScraper,
)

RESOLVES = [
    # Path-based tenants
    (
        "https://www.applitrack.com/leander/onlineapp/"
        "JobPostings/view.asp?AppliTrackJobId=10217",
        ATSType.APPLITRACK,
        "https://www.applitrack.com/leander/onlineapp",
    ),
    (
        "https://phl.applitrack.com/RESA/onlineapp/"
        "JobPostings/view.asp?AppliTrackJobId=15692_1164",
        ATSType.APPLITRACK,
        "https://www.applitrack.com/resa/onlineapp",
    ),
    ("https://jobs.ashbyhq.com/openai", ATSType.ASHBY, "openai"),
    ("https://jobs.ashbyhq.com/openai/some-posting-id", ATSType.ASHBY, "openai"),
    ("https://boards.greenhouse.io/anthropic", ATSType.GREENHOUSE, "anthropic"),
    ("https://job-boards.greenhouse.io/anthropic/jobs/123", ATSType.GREENHOUSE, "anthropic"),
    ("https://boards.eu.greenhouse.io/acme", ATSType.GREENHOUSE, "acme"),
    ("https://jobs.lever.co/palantir", ATSType.LEVER, "palantir"),
    # Lever's EU region has its own API: the slug keeps the board URL.
    ("https://jobs.eu.lever.co/acme", ATSType.LEVER, "https://jobs.eu.lever.co/acme"),
    (
        "https://jobs.eu.lever.co/acme/1b2c3d4e-0000/apply",
        ATSType.LEVER,
        "https://jobs.eu.lever.co/acme",
    ),
    # Lever board slugs also use "&"; the scraper escapes it.
    ("https://jobs.lever.co/1840&Company", ATSType.LEVER, "1840&Company"),
    ("https://jobs.lever.co/1840%26Company/abc", ATSType.LEVER, "1840&Company"),
    (
        "https://jobs.eu.lever.co/1840&Company",
        ATSType.LEVER,
        "https://jobs.eu.lever.co/1840%26Company",
    ),
    ("https://apply.workable.com/0x/", ATSType.WORKABLE, "0x"),
    ("https://careers.smartrecruiters.com/10Pearls", ATSType.SMARTRECRUITERS, "10Pearls"),
    ("https://jobs.gem.com/accel", ATSType.GEM, "accel"),
    ("https://ats.rippling.com/acme/jobs", ATSType.RIPPLING, "acme"),
    ("https://apply.clearcompany.com/avifoodsystems", ATSType.CLEARCOMPANY, "avifoodsystems"),
    (
        "https://apply.clearcompany.com/peopleinc-fr?jobId=dc63d201-4976-93f9-e3a0-27b849fc30d3",
        ATSType.CLEARCOMPANY,
        "peopleinc-fr",
    ),
    # ClearCompany's earlier address redirects to the career site of the
    # same name.
    (
        "https://avifoodsystems.hrmdirect.com/employment/job-openings.php",
        ATSType.CLEARCOMPANY,
        "avifoodsystems",
    ),
    ("https://utah.peopleadmin.com/postings/search", ATSType.PEOPLEADMIN, "utah"),
    ("https://jobs-shepherd.peopleadmin.com/postings/9286", ATSType.PEOPLEADMIN, "jobs-shepherd"),
    (
        "https://edf-recrute.talent-soft.com/offre-de-emploi/liste-toutes-offres.aspx",
        ATSType.TALENTSOFT,
        "edf-recrute",
    ),
    (
        "https://aerlingus-career.talent-soft.com/job/job-buyer_2069.aspx",
        ATSType.TALENTSOFT,
        "aerlingus-career",
    ),
    ("https://emergitel.catsone.com/careers/", ATSType.CATSONE, "emergitel"),
    (
        "https://emergitel.catsone.com/careers/7701/jobs/16866544-Marketing-Coordinator",
        ATSType.CATSONE,
        "emergitel/7701",
    ),
    ("https://emergitel.catsone.com/careers/7701-General", ATSType.CATSONE, "emergitel/7701"),
    ("https://herp.careers/v1/herpinc", ATSType.HERP, "herpinc"),
    ("https://herp.careers/v1/herpinc/abc123", ATSType.HERP, "herpinc"),
    ("https://hrmos.co/pages/ykk/jobs", ATSType.HRMOS, "ykk"),
    ("https://hrmos.co/pages/ykk/jobs/00-KU-001", ATSType.HRMOS, "ykk"),
    (
        "https://100.keka.com/careers/jobdetails/153027",
        ATSType.KEKA,
        "https://100.keka.com/careers",
    ),
    (
        "https://100.keka.com/careers/applyjob/153027",
        ATSType.KEKA,
        "https://100.keka.com/careers",
    ),
    (
        "https://universaled.keka.com/careers/ebenezerschool/jobdetails/123",
        ATSType.KEKA,
        "https://universaled.keka.com/careers/ebenezerschool",
    ),
    (
        "https://universaled.keka.com/careers/ebenezerschool/applyjob/123",
        ATSType.KEKA,
        "https://universaled.keka.com/careers/ebenezerschool",
    ),
    (
        "https://www.paycomonline.net/v4/ats/web.php/portal/"
        "00F1F305D986350F2A5DF3D1AE79350F/career-page",
        ATSType.PAYCOM,
        "00f1f305d986350f2a5df3d1ae79350f",
    ),
    (
        "https://www.paycomonline.net/v4/ats/web.php/portal/"
        "00F1F305D986350F2A5DF3D1AE79350F/jobs/466263",
        ATSType.PAYCOM,
        "00f1f305d986350f2a5df3d1ae79350f",
    ),
    (
        "https://abeking.career.softgarden.de/jobs/60111660/example",
        ATSType.SOFTGARDEN,
        "abeking",
    ),
    ("https://jobs.jobvite.com/sitecore", ATSType.JOBVITE, "sitecore"),
    (
        "https://jobs.jobvite.com/careers/loandepot/search?p=0",
        ATSType.JOBVITE,
        "careers/loandepot",
    ),
    (
        "https://careers.pageuppeople.com/513/cw/en/listing/",
        ATSType.PAGEUP,
        "513/cw/en",
    ),
    (
        "https://careers.pageuppeople.com/mob/1078/cw/en/job/123/title",
        ATSType.PAGEUP,
        "1078/cw/en",
    ),
    (
        "https://recruiting2.ultipro.com/HEN1009HPCC/JobBoard/"
        "b27ab828-18a9-4f10-8ee1-8259de6c9e73/OpportunityDetail",
        ATSType.UKG,
        "https://recruiting2.ultipro.com/HEN1009HPCC/JobBoard/"
        "b27ab828-18a9-4f10-8ee1-8259de6c9e73",
    ),
    (
        "https://recruiting.paylocity.com/Recruiting/Jobs/All/"
        "8e0feae7-e42f-437e-97b1-53b917185eed",
        ATSType.PAYLOCITY,
        "8e0feae7-e42f-437e-97b1-53b917185eed",
    ),
    ("https://join.com/companies/acme", ATSType.JOIN_COM, "acme"),
    (
        "https://jobs.dayforcehcm.com/en-CA/mayfair/CANDIDATEPORTAL/jobs/3612",
        ATSType.DAYFORCE,
        "mayfair/CANDIDATEPORTAL",
    ),
    (
        "https://jobs.dayforcehcm.com/fi/CANDIDATEPORTAL",
        ATSType.DAYFORCE,
        "fi/CANDIDATEPORTAL",
    ),
    (
        "https://jobs.dayforcehcm.com/gm/CANDIDATEPORTAL/jobs",
        ATSType.DAYFORCE,
        "gm/CANDIDATEPORTAL",
    ),
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
    (
        "https://dfmc.hotjob.cn/SU61d501d92f9d24431f65f608/mc/detail"
        "?postId=6a3cd0971d4c30777af06d79&recruitType=12",
        ATSType.WINTALENT,
        "https://dfmc.hotjob.cn/SU61d501d92f9d24431f65f608",
    ),
    (
        "https://www.hotjob.cn/wt/caict/web/index",
        ATSType.WINTALENT,
        "https://www.hotjob.cn/wt/caict",
    ),
    # Subdomain tenants
    ("https://12build.recruitee.com", ATSType.RECRUITEE, "12build"),
    ("https://1komma5.teamtailor.com/jobs", ATSType.TEAMTAILOR, "1komma5"),
    ("https://acme.breezy.hr", ATSType.BREEZY, "acme"),
    ("https://10web.bamboohr.com/careers", ATSType.BAMBOOHR, "10web"),
    ("https://10xfounders.jobs.personio.com", ATSType.PERSONIO, "10xfounders"),
    ("https://aawdc.pinpointhq.com", ATSType.PINPOINT, "aawdc"),
    ("https://acme.applytojob.com/apply", ATSType.JAZZHR, "acme"),
    (
        "https://kirkhill.applicantpro.com/jobs/4124648",
        ATSType.APPLICANTPRO,
        "kirkhill",
    ),
    (
        "https://careers.hireology.com/andersonautogroup/2827500/description",
        ATSType.HIREOLOGY,
        "andersonautogroup",
    ),
    (
        "https://careers.hireology.com/thelodgenursing%26rehabcenter/",
        ATSType.HIREOLOGY,
        "thelodgenursing&rehabcenter",
    ),
    (
        # A child location: the slash inside the tenant name is encoded.
        "https://careers.hireology.com/"
        "brandthamptoninn%26suitesprovidence%2Fsmithfield/2827500/description",
        ATSType.HIREOLOGY,
        "brandthamptoninn&suitesprovidence/smithfield",
    ),
    ("https://bloomberg.avature.net/careers", ATSType.AVATURE, "bloomberg"),
    ("https://nvidia.eightfold.ai/careers", ATSType.EIGHTFOLD, "nvidia"),
    ("https://petz.gupy.io/jobs/123", ATSType.GUPY, "petz"),
    (
        "https://day1company.ninehire.site/job_posting/7a0UlD8L",
        ATSType.NINEHIRE,
        "day1company",
    ),
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


@pytest.mark.parametrize(
    "url",
    [
        "https://valero.taleo.net/careersection/2/jobsearch.ftl",
        "https://valero.taleo.net/careersection/2/jobsearch.ftl?lang=en",
        "https://VALERO.taleo.net/careersection/2/jobdetail.ftl?job=26001GK&lang=en",
        "valero.taleo.net/careersection/2",
        "https://valero.taleo.net/careersection/2/",
    ],
)
def test_taleo_enterprise_slug_is_zone_host_and_section(url: str) -> None:
    resolved = resolve_careers_url(url)
    assert resolved is not None
    assert resolved.ats is ATSType.TALEO_ENTERPRISE
    assert resolved.slug == "valero.taleo.net/2"


def test_taleo_enterprise_section_keeps_its_case() -> None:
    resolved = resolve_careers_url(
        "https://aa080.taleo.net/careersection/SDP_EXTERNAL_CAREER_SECTION/jobsearch.ftl"
    )
    assert resolved is not None
    assert resolved.slug == "aa080.taleo.net/SDP_EXTERNAL_CAREER_SECTION"


@pytest.mark.parametrize(
    "url",
    [
        # A zone without a career section cannot be scraped.
        "https://valero.taleo.net",
        "https://valero.taleo.net/careersection/",
        # The search endpoint is not a career section.
        "https://valero.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal=1",
        "https://valero.taleo.net/careersection/rest",
        "https://valero.taleo.net/other/2/jobsearch.ftl",
        # The whole path is checked, not only its start.
        "https://valero.taleo.net/careersection/2/../admin",
        "https://valero.taleo.net/careersection/2/sub/jobsearch.ftl",
        "https://valero.taleo.net/careersection/2/jobsearch.ftl/extra",
    ],
)
def test_taleo_enterprise_url_without_a_section_is_not_resolved(url: str) -> None:
    assert resolve_careers_url(url) is None


def test_taleo_business_edition_is_not_taken_for_enterprise() -> None:
    resolved = resolve_careers_url(
        "https://phe.tbe.taleo.net/phe01/ats/careers/v2/searchResults?org=ACME&cws=41"
    )
    assert resolved is not None
    assert resolved.ats is ATSType.TALEO


def test_get_scraper_for_url_builds_taleo_enterprise_scraper() -> None:
    from ats_scrapers.scrapers import TaleoEnterpriseScraper

    scraper = get_scraper_for_url(
        "https://valero.taleo.net/careersection/2/jobsearch.ftl?lang=en"
    )

    assert isinstance(scraper, TaleoEnterpriseScraper)
    assert (scraper.host, scraper.section) == ("valero.taleo.net", "2")


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
        "https://www.applitrack.com/onlineapp/JobPostings/view.asp",
        "https://www.applitrack.com/bad_tenant/onlineapp/",
        "https://www.applitrack.com/leander/jobs/",
        "https://foo.bar.applitrack.com/leander/onlineapp/",
        "https://jobs.ashbyhq.com",               # no slug in path
        "https://jobs.jobvite.com/search",
        "https://jobs.jobvite.com/jobs",
        "https://jobs.jobvite.com/viewall",
        "https://jobs.jobvite.com/careers/search",
        "https://join.com/about",                 # join.com non-company path
        "https://herp.careers/careers/jobs/abc",
        "https://herp.careers/v1/bad_slug",
        "https://hrmos.co/pages/bad_slug/jobs",
        "https://bad_slug.keka.com/careers",
        "https://100.keka.com/api/jobs",
        "https://100.keka.com/careers/jobdetails/not-a-number",
        "https://100.keka.com/careers/custom/unknown/123",
        "https://www.paycomonline.net/v4/ats/web.php/portal/not-a-token/jobs/1",
        "https://evil.recruitee.com.attacker.io", # suffix-spoofing host
        "https://evil.career.softgarden.de.attacker.io/jobs/1",
        "https://bad_slug.darwinbox.in/ms/candidate/careers",
        f"https://{'a' * 64}.darwinbox.com/ms/candidate/careers",
        "https://trailing-.darwinbox.in/ms/candidate/careers",
        "https://hotjob.cn/not-a-suite",
        "https://evil.hotjob.cn.attacker.io/SU61d501d92f9d24431f65f608",
        "https://dfmc.hotjob.cn/SU-not-valid",
        "https://www.hotjob.cn/wt/-caict/web/index",
        "https://www.hotjob.cn/wt/bad%20tenant/web/index",
        "https://bad_slug.zhiye.com/social/jobs",
        "https://recruiting.ultipro.com/bad-tenant/JobBoard/"
        "11111111-2222-3333-4444-555555555555",
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4363000",
        "https://jobs.dayforcehcm.com/en-CA/mayfair/"
        "CANDIDATEPORTAL/search",
        f"https://{'a' * 64}.zhiye.com/social/jobs",
        "not a url at all",
    ],
)
def test_unrecognized_urls_return_none(url: str) -> None:
    assert resolve_careers_url(url) is None


def test_get_scraper_for_url_builds_scraper() -> None:
    assert isinstance(
        get_scraper_for_url(
            "https://www.applitrack.com/leander/onlineapp/"
            "JobPostings/view.asp?AppliTrackJobId=10217"
        ),
        AppliTrackScraper,
    )
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
    assert isinstance(
        get_scraper_for_url("https://herp.careers/v1/herpinc"),
        HerpScraper,
    )
    assert isinstance(
        get_scraper_for_url("https://hrmos.co/pages/ykk/jobs"),
        HrmosScraper,
    )
    assert isinstance(
        get_scraper_for_url(
            "https://www.paycomonline.net/v4/ats/web.php/portal/"
            "00F1F305D986350F2A5DF3D1AE79350F/career-page"
        ),
        PaycomScraper,
    )
    assert isinstance(get_scraper_for_url("https://petz.gupy.io"), GupyScraper)
    assert isinstance(
        get_scraper_for_url("https://jobs.jobvite.com/sitecore"),
        JobviteScraper,
    )
    assert isinstance(
        get_scraper_for_url("https://abeking.career.softgarden.de/jobs"),
        SoftgardenScraper,
    )
    assert (
        get_scraper_for_url(
            "https://jobs.jobvite.com/careers/loandepot/search"
        ).tenant_path
        == "careers/loandepot"
    )
    assert isinstance(
        get_scraper_for_url(
            "https://careers.pageuppeople.com/513/cw/en/listing/"
        ),
        PageUpScraper,
    )
    assert isinstance(
        get_scraper_for_url(
            "https://recruiting.ultipro.ca/ROY5000RCPS/JobBoard/"
            "9a9d6241-e23f-47c0-80f3-e43a148972a0"
        ),
        UKGProScraper,
    )
    assert isinstance(
        get_scraper_for_url(
            "https://recruiting.paylocity.com/Recruiting/Jobs/All/"
            "8e0feae7-e42f-437e-97b1-53b917185eed"
        ),
        PaylocityScraper,
    )
    assert isinstance(
        get_scraper_for_url(
            "https://jobs.dayforcehcm.com/mayfair/CANDIDATEPORTAL"
        ),
        DayforceScraper,
    )
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
    assert isinstance(
        get_scraper_for_url(
            "https://day1company.ninehire.site/job_posting/7a0UlD8L"
        ),
        NinehireScraper,
    )
    assert isinstance(
        get_scraper_for_url(
            "https://dfmc.hotjob.cn/SU61d501d92f9d24431f65f608"
        ),
        WinTalentScraper,
    )
    legacy_wintalent = get_scraper_for_url(
        "https://www.hotjob.cn/wt/caict/web/index"
    )
    assert isinstance(legacy_wintalent, WinTalentScraper)
    assert legacy_wintalent.variant == "legacy"
    assert legacy_wintalent.portal_url == "https://www.hotjob.cn/wt/caict"
    workday = get_scraper_for_url(
        "https://2020companies.wd1.myworkdayjobs.com/external_careers"
    )
    assert isinstance(workday, WorkdayScraper)


def test_get_scraper_for_keka_url_builds_scraper() -> None:
    scraper = get_scraper_for_url("https://100.keka.com/careers")

    assert isinstance(scraper, KekaScraper)
    assert scraper.company_slug == "https://100.keka.com/careers"


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


def test_get_scraper_for_url_keeps_the_lever_region() -> None:
    from ats_scrapers.scrapers import LeverScraper

    eu = get_scraper_for_url("https://jobs.eu.lever.co/acme/1b2c3d4e-0000")
    us = get_scraper_for_url("https://jobs.lever.co/acme/1b2c3d4e-0000")

    assert isinstance(eu, LeverScraper)
    assert (eu.tenant, eu.region) == ("acme", "eu")
    assert isinstance(us, LeverScraper)
    assert (us.tenant, us.region) == ("acme", "us")


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co",
        "https://jobs.eu.lever.co/",
        "https://jobs.lever.co/%2e%2e/admin",
        "https://jobs.eu.lever.co/a%2Fb",
        "https://jobs.lever.co/a b",
        "https://jobs.eu.lever.co/-acme",
        # Decoded control characters are not part of a slug.
        "https://jobs.lever.co/acme%0A",
        "https://jobs.eu.lever.co/acme%0A",
        "https://jobs.eu.lever.co/acme%0D%0A/abc",
    ],
)
def test_lever_url_without_a_usable_slug_is_not_resolved(url: str) -> None:
    assert resolve_careers_url(url) is None


def test_get_scraper_for_url_escapes_an_unusual_lever_slug() -> None:
    scraper = get_scraper_for_url("https://jobs.eu.lever.co/1840&Company/abc")

    assert (scraper.tenant, scraper.region) == ("1840&Company", "eu")


def test_get_scraper_for_url_builds_clearcompany_scraper() -> None:
    from ats_scrapers.scrapers import ClearCompanyScraper

    scraper = get_scraper_for_url("https://apply.clearcompany.com/avifoodsystems?jobId=1")

    assert isinstance(scraper, ClearCompanyScraper)
    assert scraper.site == "avifoodsystems"


def test_clearcompany_tenant_host_is_not_resolved() -> None:
    """``{tenant}.clearcompany.com`` is the application portal; it does not
    name the career site."""
    assert resolve_careers_url("https://acme.clearcompany.com/careers/jobs/1/apply") is None
    assert resolve_careers_url("https://apply.clearcompany.com/") is None


def test_get_scraper_for_url_builds_peopleadmin_scraper() -> None:
    from ats_scrapers.scrapers import PeopleAdminScraper

    scraper = get_scraper_for_url("https://utah.peopleadmin.com/postings/211962")

    assert isinstance(scraper, PeopleAdminScraper)
    assert scraper.tenant == "utah"
    assert scraper.base_url == "https://utah.peopleadmin.com"


def test_peopleadmin_hosts_that_are_not_a_portal_are_not_resolved() -> None:
    """The vendor's own site, and the hosts under ``ted.peopleadmin.com``
    (school-district sites that moved to another product)."""
    assert resolve_careers_url("https://peopleadmin.com/products/") is None
    assert resolve_careers_url("https://www.peopleadmin.com/products/") is None
    assert resolve_careers_url("https://district.ted.peopleadmin.com/hire/index.aspx") is None


def test_get_scraper_for_url_builds_talentsoft_scraper() -> None:
    from ats_scrapers.scrapers import TalentsoftScraper

    scraper = get_scraper_for_url(
        "https://dassault-aviation-cand.talent-soft.com/offre-de-emploi/emploi-acheteur_15653.aspx"
    )

    assert isinstance(scraper, TalentsoftScraper)
    assert scraper.site == "dassault-aviation-cand"
    assert scraper.base_url == "https://dassault-aviation-cand.talent-soft.com"


def test_talentsoft_hosts_that_are_not_a_career_site_are_not_resolved() -> None:
    assert resolve_careers_url("https://talent-soft.com/") is None
    assert resolve_careers_url("https://www.talent-soft.com/") is None
    assert resolve_careers_url("https://a.b.talent-soft.com/offre-de-emploi/") is None


def test_get_scraper_for_url_builds_catsone_scraper() -> None:
    from ats_scrapers.scrapers import CatsOneScraper

    scraper = get_scraper_for_url("https://emergitel.catsone.com/careers/7701-General")

    assert isinstance(scraper, CatsOneScraper)
    assert (scraper.tenant, scraper.portal) == ("emergitel", "7701")
    assert scraper.base_url == "https://emergitel.catsone.com"


def test_catsone_hosts_that_are_not_a_tenant_are_not_resolved() -> None:
    assert resolve_careers_url("https://catsone.com/careers/") is None
    assert resolve_careers_url("https://www.catsone.com/pricing/") is None
    assert resolve_careers_url("https://a.b.catsone.com/careers/") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://emergitel.catsone.com",
        "https://emergitel.catsone.com/",
        "https://emergitel.catsone.com/careers",
        "https://emergitel.catsone.com/careers/",
    ],
)
def test_a_catsone_tenant_address_is_its_default_portal(url: str) -> None:
    resolved = resolve_careers_url(url)

    assert resolved is not None
    assert (resolved.ats, resolved.slug) == (ATSType.CATSONE, "emergitel")


@pytest.mark.parametrize(
    "url",
    [
        "https://emergitel.catsone.com/careers/index.php?m=portal&portalID=7701",
        "https://emergitel.catsone.com/careers/General",
        "https://emergitel.catsone.com/careers/x7701",
        "https://emergitel.catsone.com/index.php?m=careers",
    ],
)
def test_a_catsone_path_that_names_no_portal_number_is_not_the_default_portal(url: str) -> None:
    """Such a URL names a page; resolving it to the default portal would read
    another portal than the one meant."""
    assert resolve_careers_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.hireology.com/",
        "https://careers.hireology.com/%2Ftenant/",
        "https://careers.hireology.com/tenant%2F/",
        "https://careers.hireology.com/tenant%2F%2Fother/",
        "https://careers.hireology.com/%2E%2E%2Fother/",
        "https://careers.hireology.com/tenant%2F..%2Fother/",
        "https://careers.hireology.com/tenant%5Cother/",
        "https://careers.hireology.com/tenant%3Fother/",
        "https://careers.hireology.com/tenant%23other/",
        "https://careers.hireology.com/tenant%0Aother/",
    ],
)
def test_hireology_url_without_a_usable_tenant_is_not_resolved(url: str) -> None:
    assert resolve_careers_url(url) is None
