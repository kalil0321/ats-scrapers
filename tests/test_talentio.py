from __future__ import annotations

import html
import json

import pytest

from ats_scrapers import get_scraper_for_url
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import TalentioScraper
from ats_scrapers.scrapers.base import ScraperRegistry

PORTAL_URL = "https://open.talentio.com/r/1/c/viewn/homes/2635"
JOB_URL = "https://open.talentio.com/r/1/c/viewn/pages/21394"
APPLY_URL = f"{JOB_URL}/apply"


def _page(group: str = "WEBサービス開発") -> dict[str, object]:
    return {
        "id": 21394,
        "name": "サーバーサイドエンジニア",
        "requisitionId": 21991,
        "formId": 1718,
        "publishedUrl": JOB_URL,
        "publishedApplyUrl": APPLY_URL,
        "_group": group,
    }


def _home_html(*, duplicate: bool = False, page_url: str = JOB_URL) -> str:
    first = _page()
    first["publishedUrl"] = page_url
    groups: list[dict[str, object]] = [
        {
            "id": 4241,
            "name": first.pop("_group"),
            "recruitmentOpenPages": [first],
        }
    ]
    if duplicate:
        second = _page("エンジニア")
        groups.append(
            {
                "id": 4242,
                "name": second.pop("_group"),
                "recruitmentOpenPages": [second],
            }
        )
    payload = {
        "openAtsCompany": {"id": 1668, "openAtsNamespace": "viewn"},
        "recruitmentOpenPageHome": {
            "id": 2635,
            "name": "募集ポジション",
            "language": "ja",
            "recruitmentPageGroups": groups,
        },
    }
    props = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return (
        "<html><head><title>募集ポジション / 株式会社ビューン</title></head>"
        '<body><div data-react-class="RecruitmentOpenPageHomeView/index.'
        f'RecruitmentOpenPageHomeView" data-react-props="{props}"></div></body></html>'
    )


def _detail_html(*, identifier: str = "21394") -> str:
    payload = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "サーバーサイドエンジニア",
        "description": "<h1>職務内容</h1><p>サービスを開発します。</p>",
        "datePosted": "2026-07-29",
        "url": JOB_URL,
        "hiringOrganization": {
            "@type": "Organization",
            "name": "株式会社ビューン",
        },
        "identifier": {
            "@type": "PropertyValue",
            "name": "株式会社ビューン",
            "value": identifier,
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "神田錦町3-13-7",
                "addressLocality": "千代田区",
                "addressRegion": "東京都",
                "addressCountry": "JP",
            },
        },
        "employmentType": "FULL_TIME",
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "JPY",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 4500000,
                "maxValue": 7000000,
                "unitText": "YEAR",
            },
        },
    }
    return (
        '<html><body><script type="application/ld+json">'
        f"{json.dumps(payload, ensure_ascii=False)}"
        "</script></body></html>"
    )


def _react_detail_html(*, include_description: bool = True) -> str:
    payload = {
        "openAtsCompany": {"id": 1668, "openAtsNamespace": "viewn"},
        "recruitmentOpenPage": {
            "id": 21394,
            "name": "サーバーサイドエンジニア",
            "requisitionId": 21991,
            "formId": 1718,
            "requisition": {"language": "ja"},
            "publishedApplyUrl": APPLY_URL,
            "publishedUrl": JOB_URL,
            "requisitionDetails": (
                [
                    {
                        "id": 1,
                        "type": "optional_field",
                        "name": "職務内容",
                        "value": "サービスを開発します。\nAPIも設計します。",
                        "selected": True,
                    },
                    {
                        "id": 2,
                        "type": "optional_field",
                        "name": "勤務地",
                        "value": ["東京都千代田区", "フルリモート可"],
                        "selected": True,
                    },
                ]
                if include_description
                else []
            ),
            "jobDescriptionDetails": [],
            "requisitionCompanyAttributes": [],
            "embeddedNote": {"htmlTags": []},
        },
        "language": "ja",
    }
    props = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return (
        "<html><head><title>サーバーサイドエンジニア / 株式会社ビューン"
        "</title></head><body><div data-react-class=\"RecruitmentOpenPageView/"
        f'index.RecruitmentOpenPageView" data-react-props="{props}"></div>'
        "</body></html>"
    )


def test_registry_resolves_talentio() -> None:
    assert ScraperRegistry.get(ATSType.TALENTIO) is TalentioScraper


def test_fetches_active_jobs_with_full_detail(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html(duplicate=True))
    httpx_mock.add_response(url=JOB_URL, text=_detail_html())

    jobs = TalentioScraper(PORTAL_URL).fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.TALENTIO
    assert job.ats_id == "21394"
    assert job.global_id == "talentio:21394"
    assert str(job.url) == JOB_URL
    assert str(job.apply_url) == APPLY_URL
    assert job.title == "サーバーサイドエンジニア"
    assert job.company == "株式会社ビューン"
    assert job.department == "WEBサービス開発; エンジニア"
    assert job.requisition_id == "21991"
    assert job.description == "<h1>職務内容</h1><p>サービスを開発します。</p>"
    assert job.location == "神田錦町3-13-7, 千代田区, 東京都, JP"
    assert job.country_iso == "JP"
    assert job.region == "Asia"
    assert job.language == "ja"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "FULL_TIME"
    assert job.salary_currency == "JPY"
    assert job.salary_period == "YEAR"
    assert job.salary_min == 4500000
    assert job.salary_max == 7000000
    assert job.posted_at is not None
    assert job.posted_at.isoformat() == "2026-07-29T00:00:00+00:00"
    assert job.raw == {"namespace": "viewn", "home_id": 2635, "form_id": 1718}


def test_listing_only_uses_catalog_company_name(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())

    job = TalentioScraper(
        PORTAL_URL,
        include_descriptions=False,
        company_name="Viewn",
    ).fetch()[0]

    assert job.company == "Viewn"
    assert job.description is None
    assert job.location is None
    assert job.country_iso is None
    assert job.region is None


def test_detail_country_is_not_forced_to_japan(httpx_mock) -> None:
    detail = _detail_html().replace(
        '"addressCountry": "JP"',
        '"addressCountry": "SG"',
    )
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, text=detail)

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.country_iso == "SG"
    assert job.region == "Asia"


def test_react_detail_fallback_preserves_legacy_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, text=_react_detail_html())

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.company == "株式会社ビューン"
    assert job.language == "ja"
    assert job.location == "東京都千代田区; フルリモート可"
    assert job.is_remote is True
    assert job.description == (
        "<h2>職務内容</h2><p>サービスを開発します。<br>APIも設計します。</p>"
        "<h2>勤務地</h2><p>東京都千代田区<br>フルリモート可</p>"
    )


def test_react_detail_without_content_is_dropped(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(
        url=JOB_URL,
        text=_react_detail_html(include_description=False),
    )

    assert TalentioScraper(PORTAL_URL).fetch() == []


def test_stale_detail_is_dropped(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, status_code=404)

    assert TalentioScraper(PORTAL_URL).fetch() == []


def test_transient_detail_failure_preserves_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    for _ in range(3):
        httpx_mock.add_response(url=JOB_URL, status_code=500)

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.ats_id == "21394"
    assert job.description is None


def test_detail_identity_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, text=_detail_html(identifier="999"))

    with pytest.raises(ScraperError, match="ID mismatch"):
        TalentioScraper(PORTAL_URL).fetch()


def test_unrecognized_home_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text="<html>maintenance</html>")

    with pytest.raises(ScraperError, match="omitted RecruitmentOpenPageHomeView"):
        TalentioScraper(PORTAL_URL).fetch()


def test_untrusted_published_url_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=PORTAL_URL,
        text=_home_html(page_url="https://evil.example/r/1/c/viewn/pages/21394"),
    )

    with pytest.raises(ScraperError, match="invalid published URL"):
        TalentioScraper(PORTAL_URL, include_descriptions=False).fetch()


@pytest.mark.parametrize(
    "url",
    [
        "viewn",
        "http://open.talentio.com/r/1/c/viewn/homes/2635",
        "https://evil.example/r/1/c/viewn/homes/2635",
        "https://open.talentio.com/r/1/c/viewn/pages/21394",
        "https://open.talentio.com/r/1/c/viewn/homes/not-a-number",
        "https://open.talentio.com/r/1/c/viewn/homes/2635?redirect=https://evil.example",
        "https://open.talentio.com.evil.example/r/1/c/viewn/homes/2635",
    ],
)
def test_rejects_invalid_portal_urls(url: str) -> None:
    with pytest.raises(ScraperError, match="TalentioScraper"):
        TalentioScraper(url)


def test_resolver_builds_talentio_scraper() -> None:
    scraper = get_scraper_for_url(f"{PORTAL_URL}/")

    assert isinstance(scraper, TalentioScraper)
    assert scraper.company_slug == PORTAL_URL


def test_resolver_rejects_detail_url() -> None:
    with pytest.raises(ScraperError, match="Could not recognize"):
        get_scraper_for_url(JOB_URL)
