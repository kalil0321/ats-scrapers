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


def _home_html(
    *,
    duplicate: bool = False,
    page_url: str = JOB_URL,
    home_url: object = PORTAL_URL,
    company: dict[str, object] | None = None,
) -> str:
    """The home page as Talentio serves it today: the tenant is named only by
    the home's own ``publishedUrl``. ``company`` adds the ``openAtsCompany``
    object older pages carried."""
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
    payload: dict[str, object] = {
        "designSetting": {"copyright": "Copyright © Viewn"},
        "recruitmentOpenPageHome": {
            "id": 2635,
            "name": "募集ポジション",
            "publishedUrl": home_url,
            "language": "ja",
            "recruitmentPageGroups": groups,
        },
        "previewMode": False,
    }
    if company is not None:
        payload["openAtsCompany"] = company
    props = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return (
        "<html><head><title>募集ポジション / 株式会社ビューン</title></head>"
        '<body><div data-react-class="RecruitmentOpenPageHomeView/index.'
        f'RecruitmentOpenPageHomeView" data-react-props="{props}"></div></body></html>'
    )


def _detail_html(
    *,
    identifier: object = "21394",
    overrides: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
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
        "identifier": (
            {
                "@type": "PropertyValue",
                "name": "株式会社ビューン",
                "value": identifier,
            }
            if isinstance(identifier, str)
            else identifier
        ),
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
    payload.update(overrides or {})
    return (
        '<html><body><script type="application/ld+json">'
        f"{json.dumps(payload, ensure_ascii=False)}"
        "</script></body></html>"
    )


def _react_detail_html(
    *,
    include_description: bool = True,
    company: dict[str, object] | None = None,
    location: list[str] | None = None,
) -> str:
    payload: dict[str, object] = {
        "companyId": 1668,
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
                        "value": location or ["東京都千代田区", "フルリモート可"],
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
    if company is not None:
        payload["openAtsCompany"] = company
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
    # The location text is kept as written; it is not read for remote work.
    assert job.is_remote is None
    assert job.description == (
        "<h2>職務内容</h2><p>サービスを開発します。<br>APIも設計します。</p>"
        "<h2>勤務地</h2><p>東京都千代田区<br>フルリモート可</p>"
    )


def test_react_detail_without_content_keeps_the_listed_job(httpx_mock) -> None:
    """The page answers and passes every identity check: the job is live, it
    only has no text. Dropping it would publish a home with jobs missing."""
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(
        url=JOB_URL,
        text=_react_detail_html(include_description=False),
    )

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.ats_id == "21394"
    assert job.description is None
    assert job.company == "株式会社ビューン"


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


# --- review fixes -------------------------------------------------------


def test_react_props_keep_content_entities() -> None:
    """BeautifulSoup decodes the attribute once; a content entity such as
    ``&quot;`` inside a JSON string must survive as text, not break JSON."""
    from ats_scrapers.scrapers.talentio import _react_payload

    props = '{"text": "say &amp;quot;hi&amp;quot;"}'
    html = (
        '<div data-react-class="RecruitmentOpenPageHomeView" '
        f"data-react-props='{props}'></div>"
    )
    assert _react_payload(html, "RecruitmentOpenPageHomeView") == {
        "text": "say &quot;hi&quot;"
    }


def test_structured_country_is_rendered_as_its_name() -> None:
    from ats_scrapers.scrapers.talentio import _location

    text, iso = _location({
        "address": {
            "addressLocality": "Tokyo",
            "addressCountry": {"@type": "Country", "name": "JP"},
        }
    })
    assert text == "Tokyo, JP"
    assert iso == "JP"


def test_aware_timestamps_are_converted_to_utc() -> None:
    from datetime import UTC, datetime

    from ats_scrapers.scrapers.talentio import _parse_datetime

    parsed = _parse_datetime("2026-07-29T09:00:00+09:00")
    assert parsed == datetime(2026, 7, 29, 0, 0, tzinfo=UTC)
    assert parsed is not None and parsed.utcoffset().total_seconds() == 0


def test_json_ld_picks_the_block_for_the_requested_job() -> None:
    import json as _json

    from ats_scrapers.scrapers.talentio import _job_posting_json_ld

    blocks = [
        {"@type": "JobPosting", "identifier": {"value": "111"}, "title": "Other"},
        {"@type": "JobPosting", "identifier": {"value": "222"}, "title": "Wanted"},
    ]
    html = f'<script type="application/ld+json">{_json.dumps(blocks)}</script>'
    picked = _job_posting_json_ld(html, "222")
    assert picked is not None and picked["title"] == "Wanted"
    fallback = _job_posting_json_ld(html, "999")
    assert fallback is not None and fallback["title"] == "Other"


# --- Tenant identity of the bootstrap ----------------------------------------


def test_home_names_its_tenant_by_its_published_url(httpx_mock) -> None:
    """Talentio's home page no longer carries ``openAtsCompany``."""
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())

    jobs = TalentioScraper(PORTAL_URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["21394"]


def test_home_with_the_older_company_object_is_still_read(httpx_mock) -> None:
    company = {"id": 1668, "openAtsNamespace": "viewn"}
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html(company=company))

    jobs = TalentioScraper(PORTAL_URL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["21394"]


@pytest.mark.parametrize(
    ("home_url", "company"),
    [
        # Another tenant's home.
        ("https://open.talentio.com/r/1/c/other/homes/2635", None),
        # The two sources disagree.
        (PORTAL_URL, {"id": 1, "openAtsNamespace": "other"}),
        ("https://open.talentio.com/r/1/c/other/homes/2635", {"openAtsNamespace": "viewn"}),
        # No usable source.
        (None, None),
        (PORTAL_URL, {"id": 1}),
        ("https://evil.example/r/1/c/viewn/homes/2635", None),
        ("https://open.talentio.com:99999/r/1/c/viewn/homes/2635", None),
        ("https://open.talentio.com/r/1/c/viewn/settings", None),
    ],
)
def test_home_of_another_tenant_fails_closed(
    httpx_mock, home_url: object, company: dict[str, object] | None
) -> None:
    httpx_mock.add_response(
        url=PORTAL_URL, text=_home_html(home_url=home_url, company=company)
    )

    with pytest.raises(ScraperError, match="namespace mismatch"):
        TalentioScraper(PORTAL_URL, include_descriptions=False).fetch()


def test_react_detail_of_another_tenant_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(
        url=JOB_URL,
        text=_react_detail_html(company={"id": 2, "openAtsNamespace": "other"}),
    )

    with pytest.raises(ScraperError, match="namespace mismatch"):
        TalentioScraper(PORTAL_URL).fetch()


def test_react_detail_fills_a_requisition_id_the_listing_lacked(httpx_mock) -> None:
    home = _home_html().replace("&quot;requisitionId&quot;: 21991, ", "")
    assert "requisitionId" not in home
    httpx_mock.add_response(url=PORTAL_URL, text=home)
    httpx_mock.add_response(url=JOB_URL, text=_react_detail_html())

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.requisition_id == "21991"


# --- Remote work ---------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        ["東京都（フルリモート不可）"],
        ["Tokyo office (no remote work)"],
        ["フルリモート"],
        ["Remote"],
    ],
)
def test_location_text_never_decides_remote_work(httpx_mock, location: list[str]) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, text=_react_detail_html(location=location))

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.location == location[0]
    assert job.is_remote is None


@pytest.mark.parametrize(
    ("location_type", "expected"),
    [
        ("TELECOMMUTE", True),
        (["TELECOMMUTE"], True),
        ("telecommute", True),
        (None, None),
        ("ONSITE", None),
        ({"@type": "Text"}, None),
    ],
)
def test_remote_work_comes_from_the_structured_flag(
    httpx_mock, location_type: object, expected: bool | None
) -> None:
    address = {"@type": "PostalAddress", "streetAddress": "フルリモート", "addressCountry": "JP"}
    overrides = {
        "jobLocationType": location_type,
        "jobLocation": {"@type": "Place", "address": address},
    }
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, text=_detail_html(overrides=overrides))

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.is_remote is expected


# --- JSON-LD shapes --------------------------------------------------------------


@pytest.mark.parametrize("type_value", [["JobPosting"], ["Thing", "JobPosting"]])
def test_json_ld_type_given_as_a_list_is_a_job_posting(httpx_mock, type_value: object) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(
        url=JOB_URL, text=_detail_html(overrides={"@type": type_value})
    )

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.description == "<h1>職務内容</h1><p>サービスを開発します。</p>"


@pytest.mark.parametrize("identifier", [21394, {"value": 21394}, {"value": " 21394 "}])
def test_json_ld_identifier_given_as_a_scalar_is_read(httpx_mock, identifier: object) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, text=_detail_html(identifier=identifier))

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert job.posted_at is not None


@pytest.mark.parametrize("identifier", [999, True, None, {"value": None}, ["21394"]])
def test_json_ld_identifier_of_another_job_fails_closed(httpx_mock, identifier: object) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(url=JOB_URL, text=_detail_html(identifier=identifier))

    with pytest.raises(ScraperError, match="ID mismatch"):
        TalentioScraper(PORTAL_URL).fetch()


@pytest.mark.parametrize(
    ("base_salary", "expected"),
    [
        # A bare number, with the unit on the amount itself.
        (
            {"currency": "JPY", "value": 5000000, "unitText": "YEAR"},
            ("JPY", "YEAR", 5000000, 5000000),
        ),
        # A bare number and no unit: the period stays unknown.
        ({"currency": "JPY", "value": 300000}, ("JPY", None, 300000, 300000)),
        # A range without unit: the period stays unknown.
        (
            {"currency": "jpy", "value": {"minValue": 250000, "maxValue": 400000}},
            ("JPY", None, 250000, 400000),
        ),
        (
            {"currency": "JPY", "value": {"value": 1500, "unitText": "hour"}},
            ("JPY", "HOUR", 1500, 1500),
        ),
        # Nothing usable.
        ({"currency": "JPY", "value": "negotiable"}, (None, None, None, None)),
        ({"currency": "JPY", "value": True}, (None, None, None, None)),
        ({"value": 5000000}, (None, None, None, None)),
    ],
)
def test_salary_shapes(
    httpx_mock, base_salary: dict[str, object], expected: tuple[object, ...]
) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_home_html())
    httpx_mock.add_response(
        url=JOB_URL, text=_detail_html(overrides={"baseSalary": base_salary})
    )

    job = TalentioScraper(PORTAL_URL).fetch()[0]

    assert (
        job.salary_currency,
        job.salary_period,
        job.salary_min,
        job.salary_max,
    ) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://open.talentio.com:port/r/1/c/viewn/homes/2635",
        "https://open.talentio.com:99999/r/1/c/viewn/homes/2635",
        "https://open.talentio.com:443/r/1/c/viewn/homes/2635",
        "https://www.open.talentio.com/r/1/c/viewn/homes/2635",
    ],
)
def test_portal_url_with_a_port_or_another_host_is_a_scraper_error(url: str) -> None:
    with pytest.raises(ScraperError, match="TalentioScraper"):
        TalentioScraper(url)
