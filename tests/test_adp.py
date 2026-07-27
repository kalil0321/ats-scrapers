from __future__ import annotations

import re

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ADPWorkforceNowScraper, ScraperRegistry

CID = "0154eb6b-035e-48d3-8281-02a72b69d53f"
CC_ID = "19000101_000001"
CAREERS_URL = (
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
    f"recruitment.html?cid={CID}&ccId={CC_ID}&lang=en_US"
)
LISTING_RE = re.compile(
    r"^https://workforcenow\.adp\.com/mascsr/default/careercenter/public/"
    r"events/staffing/v1/job-requisitions\?"
)
DETAIL_RE = re.compile(
    r"^https://workforcenow\.adp\.com/mascsr/default/careercenter/public/"
    r"events/staffing/v1/job-requisitions/596289\?"
)


def _item(
    *,
    item_id: str = "9202933508020_1",
    title: str = "Safety Manager (part-time)",
    external_job_id: str = "596289",
) -> dict[str, object]:
    return {
        "itemID": item_id,
        "requisitionTitle": title,
        "postDate": "2026-07-24T15:38:00.000-04:00",
        "clientRequisitionID": "1751",
        "requisitionLocations": [
            {
                "address": {
                    "cityName": "Brooklyn Park",
                    "countrySubdivisionLevel1": {"codeValue": "MN"},
                    "postalCode": "55428",
                },
                "nameCode": {
                    "shortName": "Brooklyn Park, Brooklyn Park, MN, US"
                },
            }
        ],
        "payGradeRange": {
            "minimumRate": {"amountValue": 40.0, "currencyCode": "USD"},
            "maximumRate": {"amountValue": 45.0, "currencyCode": "USD"},
        },
        "workLevelCode": {"shortName": "Part-Time"},
        "customFieldGroup": {
            "codeFields": [
                {
                    "codeValue": "RANGE",
                    "shortName": "RANGE",
                    "nameCode": {"codeValue": "SalaryRangeType"},
                },
                {
                    "codeValue": "HR",
                    "shortName": "Hourly",
                    "nameCode": {"codeValue": "SalaryType"},
                },
            ],
            "stringFields": [
                {
                    "stringValue": "Manager",
                    "nameCode": {"codeValue": "JobClass"},
                },
                {
                    "stringValue": external_job_id,
                    "nameCode": {"codeValue": "ExternalJobID"},
                },
                {
                    "stringValue": "40 To 45 (USD) Hourly",
                    "nameCode": {"codeValue": "SalaryRange"},
                },
                {
                    "stringValue": "Operations",
                    "nameCode": {"codeValue": "HomeDepartment"},
                },
            ],
        },
    }


def _page(
    items: list[dict[str, object]],
    *,
    total: int | None = None,
) -> dict[str, object]:
    return {
        "jobRequisitions": items,
        "meta": {"startSequence": 0, "totalNumber": len(items) if total is None else total},
    }


def _detail(
    *,
    item_id: str = "9202933508020_1",
    description: str = "<div><p>Support <strong>families</strong>.</p><p>Apply now.</p></div>",
) -> dict[str, object]:
    return {
        **_item(item_id=item_id),
        "requisitionDescription": description,
    }


def test_registry_maps_adp_provider() -> None:
    assert ScraperRegistry.get(ATSType.ADP) is ADPWorkforceNowScraper


def test_fetches_listing_and_detail_with_structured_fields(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_RE, json=_page([_item()]))
    httpx_mock.add_response(url=DETAIL_RE, json=_detail())

    [job] = ADPWorkforceNowScraper(
        CAREERS_URL,
        company_name="Second Harvest Heartland",
    ).fetch()

    assert job.company == "Second Harvest Heartland"
    assert job.ats_id == f"{CID}:{CC_ID}:9202933508020_1"
    assert job.global_id == f"adp:{CID}:{CC_ID}:9202933508020_1"
    assert job.requisition_id == "1751"
    assert job.title == "Safety Manager (part-time)"
    assert job.location == "Brooklyn Park, Brooklyn Park, MN, US"
    assert job.country_iso == "US"
    assert job.region == "North America"
    assert job.language == "en"
    assert job.salary_min == 40
    assert job.salary_max == 45
    assert job.salary_currency == "USD"
    assert job.salary_period == "HOUR"
    assert job.salary_summary == "40 To 45 (USD) Hourly"
    assert job.employment_type == "PART_TIME"
    assert job.commitment == "Part-Time"
    assert job.department == "Operations"
    assert job.description == "Support families. Apply now."
    assert job.posted_at is not None
    assert job.raw == {
        "item_id": "9202933508020_1",
        "external_job_id": "596289",
        "job_class": "Manager",
        "salary_type_code": "HR",
        "salary_type_label": "Hourly",
    }
    assert f"cid={CID}" in str(job.url)
    assert f"ccId={CC_ID}" in str(job.url)
    assert "jobId=9202933508020_1" in str(job.url)

    listing_request, detail_request = httpx_mock.get_requests()
    assert listing_request.headers["x-forwarded-host"] == "workforcenow.adp.com"
    assert listing_request.headers["locale"] == "en_US"
    assert listing_request.url.params["$top"] == "100"
    assert listing_request.url.params["$skip"] == "0"
    assert detail_request.url.params["ccId"] == CC_ID


def test_include_descriptions_false_skips_detail(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_RE, json=_page([_item()]))

    [job] = ADPWorkforceNowScraper(
        CAREERS_URL,
        include_descriptions=False,
    ).fetch()

    assert job.description is None
    assert len(httpx_mock.get_requests()) == 1


def test_get_description_uses_external_job_id(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_RE, json=_page([_item()]))
    scraper = ADPWorkforceNowScraper(
        CAREERS_URL,
        include_descriptions=False,
    )
    [job] = scraper.fetch()
    httpx_mock.add_response(url=DETAIL_RE, json=_detail())

    description = scraper.get_description(job)

    assert description == "Support families. Apply now."
    assert job.description is None


def test_paginates_until_advertised_total(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_RE,
        json=_page([_item(item_id="1"), _item(item_id="2")], total=3),
    )
    httpx_mock.add_response(
        url=LISTING_RE,
        json=_page([_item(item_id="3")], total=3),
    )

    jobs = ADPWorkforceNowScraper(
        CAREERS_URL,
        include_descriptions=False,
    ).fetch()

    assert [job.ats_id for job in jobs] == [
        f"{CID}:{CC_ID}:1",
        f"{CID}:{CC_ID}:2",
        f"{CID}:{CC_ID}:3",
    ]
    assert [request.url.params["$skip"] for request in httpx_mock.get_requests()] == [
        "0",
        "2",
    ]


def test_early_empty_page_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_RE,
        json=_page([_item(item_id="1")], total=2),
    )
    httpx_mock.add_response(url=LISTING_RE, json=_page([], total=2))

    with pytest.raises(ScraperError, match="pagination ended"):
        ADPWorkforceNowScraper(
            CAREERS_URL,
            include_descriptions=False,
        ).fetch()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"jobRequisitions": [], "meta": {}},
        {"jobRequisitions": "not-a-list", "meta": {"totalNumber": 1}},
        {"jobRequisitions": [], "meta": {"totalNumber": -1}},
    ],
)
def test_malformed_listing_fails_closed(httpx_mock, payload) -> None:
    httpx_mock.add_response(url=LISTING_RE, json=payload)

    with pytest.raises(ScraperError, match="ADP listing response"):
        ADPWorkforceNowScraper(
            CAREERS_URL,
            include_descriptions=False,
        ).fetch()


def test_conflicting_duplicate_id_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_RE,
        json=_page(
            [
                _item(item_id="1", title="Engineer"),
                _item(item_id="1", title="Accountant"),
            ]
        ),
    )

    with pytest.raises(ScraperError, match="conflicting duplicate"):
        ADPWorkforceNowScraper(
            CAREERS_URL,
            include_descriptions=False,
        ).fetch()


def test_detail_failure_keeps_listing_row(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_RE, json=_page([_item()]))
    httpx_mock.add_response(url=DETAIL_RE, status_code=404)

    [job] = ADPWorkforceNowScraper(CAREERS_URL).fetch()

    assert job.title == "Safety Manager (part-time)"
    assert job.description is None


def test_mismatched_detail_item_id_is_ignored(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_RE, json=_page([_item()]))
    httpx_mock.add_response(url=DETAIL_RE, json=_detail(item_id="different"))

    [job] = ADPWorkforceNowScraper(CAREERS_URL).fetch()

    assert job.description is None


def test_detail_without_item_id_is_ignored(httpx_mock) -> None:
    detail = _detail()
    detail.pop("itemID")
    httpx_mock.add_response(url=LISTING_RE, json=_page([_item()]))
    httpx_mock.add_response(url=DETAIL_RE, json=detail)

    [job] = ADPWorkforceNowScraper(CAREERS_URL).fetch()

    assert job.description is None


def test_placeholder_language_defaults_to_en_us(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_RE, json=_page([_item()]))
    url = CAREERS_URL.replace("lang=en_US", "lang=undefined")

    [job] = ADPWorkforceNowScraper(
        url,
        include_descriptions=False,
    ).fetch()

    assert job.language == "en"
    [request] = httpx_mock.get_requests()
    assert request.headers["locale"] == "en_US"
    assert request.url.params["lang"] == "en_US"


def test_multiple_locations_preserved_and_remote_detected(httpx_mock) -> None:
    item = _item()
    item["requisitionLocations"] = [
        {"nameCode": {"shortName": "Remote, Remote, US"}},
        {"nameCode": {"shortName": "Brooklyn Park, MN, US"}},
    ]
    httpx_mock.add_response(url=LISTING_RE, json=_page([item]))

    [job] = ADPWorkforceNowScraper(
        CAREERS_URL,
        include_descriptions=False,
    ).fetch()

    assert job.location == "Remote, Remote, US; Brooklyn Park, MN, US"
    assert job.is_remote is True
    assert job.raw is not None
    assert job.raw["all_locations"] == [
        "Remote, Remote, US",
        "Brooklyn Park, MN, US",
    ]


def test_inverted_positive_salary_range_fails_closed(httpx_mock) -> None:
    item = _item()
    item["payGradeRange"] = {
        "minimumRate": {"amountValue": 50, "currencyCode": "USD"},
        "maximumRate": {"amountValue": 40, "currencyCode": "USD"},
    }
    httpx_mock.add_response(url=LISTING_RE, json=_page([item]))

    with pytest.raises(ScraperError, match="inverted positive salary"):
        ADPWorkforceNowScraper(
            CAREERS_URL,
            include_descriptions=False,
        ).fetch()


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "https://example.com/recruitment.html?cid=x&ccId=y",
        "https://workforcenow.adp.com/recruitment.html?cid=x",
        "https://workforcenow.adp.com/recruitment.html?ccId=y",
    ],
)
def test_rejects_invalid_target_urls(url: str) -> None:
    with pytest.raises(ScraperError, match="ADP"):
        ADPWorkforceNowScraper(url).fetch()
