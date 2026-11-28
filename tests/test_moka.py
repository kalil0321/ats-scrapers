"""Tests for the Moka (mokahr.com) scraper."""

from __future__ import annotations

import base64
import json

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import MokaScraper, ScraperRegistry
from ats_scrapers.scrapers.moka import decrypt_moka

URL_APP = "https://app.mokahr.com/api/outer/ats-apply/website/jobs/v2"
URL_HIRE_R1 = "https://hire-r1.mokahr.com/api/outer/ats-apply/website/jobs/v2"
KEY = "1234567890abcdef"
EMPTY_CIPHER = (
    "EzXcB1Inxi9rKIuYZb9XL/cjLEe0wCdwOB+BD8ot+uNfzl3LzdmG/Mkj+ipAdC9j9"
    "foq9xMFpcVF+u4d9emRrP10dylE9SKm/c2pxowFTJU="
)


def job(job_id: str = "job-1", title: str = "Senior Backend Engineer") -> dict:
    return {
        "id": job_id,
        "title": title,
        "jobDescription": "<p>Build distributed systems.</p>",
        "locations": [
            {
                "cityName": "上海市",
                "provinceName": "上海",
                "country": "中国",
            }
        ],
        "publishedAt": "2026-04-30T18:29:13",
        "status": "open",
        "department": {"id": 1, "name": "Engineering"},
        "commitment": "全职",
        "multiLocale": '{"mainLocale":"zh-CN"}',
    }


def envelope(jobs: list[dict] | None = None, *, total: int | None = None) -> dict:
    items = [job()] if jobs is None else jobs
    return {
        "code": 0,
        "success": True,
        "data": {
            "jobStats": {"total": len(items) if total is None else total},
            "jobs": items,
        },
    }


def test_decrypt_moka_pinned_fixture() -> None:
    assert decrypt_moka(EMPTY_CIPHER, KEY) == envelope([])


def test_decrypt_moka_rejects_bad_base64() -> None:
    with pytest.raises(ScraperError, match="not valid base64"):
        decrypt_moka("not::base64", KEY)


def test_decrypt_moka_rejects_short_key() -> None:
    with pytest.raises(ScraperError, match="invalid byte length"):
        decrypt_moka(EMPTY_CIPHER, "short")


def test_decrypt_moka_rejects_truncated_ciphertext() -> None:
    raw = base64.b64decode(EMPTY_CIPHER)[:-1]
    with pytest.raises(ScraperError, match="not a multiple of 16"):
        decrypt_moka(base64.b64encode(raw).decode(), KEY)


def test_registry_resolves_moka() -> None:
    assert ScraperRegistry.get(ATSType.MOKA) is MokaScraper


@pytest.mark.parametrize(
    ("slug", "match"),
    [
        ("trip", "must be"),
        ("trip/not-a-number", "siteId must be numeric"),
        ("trip/70415/internal", "recruitment type"),
        ("trip/70415/social/extra", "unexpected segments"),
    ],
)
def test_rejects_invalid_slugs(slug: str, match: str) -> None:
    with pytest.raises(ScraperError, match=match):
        MokaScraper(slug)


def test_parses_host_and_recruitment_type() -> None:
    scraper = MokaScraper("hire-r1/klookcareers/100008011/campus")
    assert scraper.slug == "klookcareers"
    assert scraper.site_id == 100008011
    assert scraper.host == "hire-r1.mokahr.com"
    assert scraper.recruitment_type == "campus"


def test_rejects_unknown_host_override() -> None:
    with pytest.raises(ScraperError, match="Unsupported Moka host"):
        MokaScraper("trip/70415", host="evil.example")


def test_parses_plaintext_envelope_end_to_end(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_APP, json=envelope())
    [result] = MokaScraper("fixturecorp/12345").fetch()

    assert result.ats_type is ATSType.MOKA
    assert result.ats_id == "job-1"
    assert result.global_id == "moka:job-1"
    assert result.title == "Senior Backend Engineer"
    assert result.company == "fixturecorp"
    assert result.location == "上海市, 上海, 中国"
    assert result.country_iso == "CN"
    assert result.region == "Asia"
    assert result.language == "zh"
    assert result.employment_type == "FULL_TIME"
    assert result.department == "Engineering"
    assert result.description == "Build distributed systems."
    assert result.posted_at is not None and result.posted_at.tzinfo is not None
    assert str(result.url) == (
        "https://app.mokahr.com/social-recruitment/fixturecorp/12345#/job/job-1"
    )
    assert result.raw == {
        "slug": "fixturecorp",
        "site_id": 12345,
        "host": "app.mokahr.com",
        "status": "open",
    }


def test_decrypts_encrypted_envelope_end_to_end(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL_APP,
        json={"data": EMPTY_CIPHER, "necromancer": KEY},
    )
    assert MokaScraper("fixturecorp/12345").fetch() == []


def test_host_prefix_targets_hire_r1(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_HIRE_R1, json=envelope([]))
    scraper = MokaScraper("hire-r1/klookcareers/100008011")
    assert scraper.fetch() == []
    [request] = httpx_mock.get_requests()
    assert request.headers["referer"] == (
        "https://hire-r1.mokahr.com/social-recruitment/klookcareers/100008011"
    )


def test_company_not_found_when_api_rejects_tenant(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL_APP,
        json={"code": 703002, "msg": "未找到对应的官网", "success": False},
    )
    with pytest.raises(CompanyNotFoundError):
        MokaScraper("missing/99999").fetch()


def test_other_api_error_is_scraper_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL_APP,
        json={"code": 500001, "msg": "boom", "success": False},
    )
    with pytest.raises(ScraperError, match="boom"):
        MokaScraper("fixturecorp/12345").fetch()


def test_paginates_and_deduplicates_jobs(httpx_mock) -> None:
    first = [job(f"job-{index}", f"Engineer {index}") for index in range(50)]
    second = [job("job-49", "Engineer 49"), job("job-50", "Engineer 50")]
    httpx_mock.add_response(url=URL_APP, json=envelope(first, total=51))
    httpx_mock.add_response(url=URL_APP, json=envelope(second, total=51))

    jobs = MokaScraper("fixturecorp/12345").fetch()

    assert len(jobs) == 51
    assert jobs[-1].ats_id == "job-50"
    requests = httpx_mock.get_requests()
    assert json.loads(requests[0].content)["offset"] == 0
    assert json.loads(requests[1].content)["offset"] == 50


def test_raises_instead_of_truncating_at_page_guard(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr("ats_scrapers.scrapers.moka.MAX_PAGES", 2)
    for page in range(2):
        rows = [job(f"job-{page}-{index}", f"Engineer {index}") for index in range(50)]
        httpx_mock.add_response(url=URL_APP, json=envelope(rows, total=150))

    with pytest.raises(ScraperError, match="page guard 2"):
        MokaScraper("fixturecorp/12345").fetch()


def test_request_body_matches_public_api(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_APP, json=envelope([]))
    MokaScraper("fixturecorp/12345").fetch()
    [request] = httpx_mock.get_requests()
    assert json.loads(request.content) == {
        "orgId": "fixturecorp",
        "siteId": 12345,
        "limit": 50,
        "offset": 0,
        "needStat": True,
        "site": "social",
    }


# --- postings the list leaves without text ------------------------------------

URL_HIRE_R1_DETAIL = "https://hire-r1.mokahr.com/api/outer/ats-apply/website/job"
URL_APP_DETAIL = "https://app.mokahr.com/api/outer/ats-apply/website/job"
TESLA_JOB_ID = "c584e57b-e74a-4f37-9ed1-1e01c1af31cb"

# A list row and its posting as hire-r1.mokahr.com serves them for
# tesla/100004142 (trimmed): the row has neither text nor locations.
TESLA_ROW = {
    "attributeId": 100000121,
    "campusSites": [],
    "commitment": "全职",
    "createdAt": "2026-10-30T15:20:04",
    "department": {"id": 100007398, "name": "Engineering and R&D"},
    "deptId": 100007398,
    "hireMode": 1,
    "id": TESLA_JOB_ID,
    "multiLocale": '{"mainLocale":"en-US","secondLocale":""}',
    "openedAt": "2026-10-30T00:00",
    "orgId": "tesla",
    "prior": 1,
    "showIsCampus": False,
    "status": "open",
    "title": "(Senior/Staff) C++ Software Engineer, Infotainment (Shanghai/Beijing Based)",
    "updatedAt": "2026-11-20T10:01:53",
    "zhineng": {"id": 100002525, "name": "Vehicle Software"},
}
TESLA_POSTING = {
    "code": 0,
    "codeType": 0,
    "msg": "",
    "success": True,
    "data": {
        **TESLA_ROW,
        "jobDescription": (
            "<p><strong>Position Description</strong></p><p>Tesla is expanding its UI "
            "software engineering team.</p><p><br></p><p><strong>Responsibilities"
            "</strong></p><p>Rapid prototyping of in-car customer experiences </p>"
        ),
        "locations": [
            {"cityId": 310115, "cityName": "Pudong", "country": "China",
             "countryDescription": "China", "id": 100015453, "provinceName": "Shanghai"},
            {"cityId": 110105, "cityName": "Chaoyang", "country": "China",
             "countryDescription": "China", "id": 100015454, "provinceName": "Beijing"},
        ],
        "locDepCampusList": [
            {"departmentIds": [100007398], "locationIds": [100015454, 100015453],
             "storeIds": []},
        ],
    },
}
TESLA_TEXT = (
    "Position Description Tesla is expanding its UI software engineering team. "
    "Responsibilities Rapid prototyping of in-car customer experiences"
)


def _encrypted(payload: dict) -> dict:
    """Wrap ``payload`` the way Moka encrypts responses for some tenants."""
    from Crypto.Cipher import AES

    plain = json.dumps(payload).encode()
    pad = 16 - len(plain) % 16
    cipher = AES.new(KEY.encode(), AES.MODE_CBC, b"de7c21ed8d6f50fe")
    data = base64.b64encode(cipher.encrypt(plain + bytes([pad]) * pad)).decode()
    return {"data": data, "necromancer": KEY}


def test_a_row_without_text_takes_its_posting(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_HIRE_R1, json=envelope([TESLA_ROW]))
    httpx_mock.add_response(url=URL_HIRE_R1_DETAIL, json=TESLA_POSTING)

    [result] = MokaScraper("hire-r1/tesla/100004142").fetch()

    assert result.description == TESLA_TEXT
    assert result.location == "Pudong, Shanghai, China; Chaoyang, Beijing, China"
    assert result.country_iso == "CN"
    assert result.region == "Asia"
    assert result.department == "Engineering and R&D"
    detail = httpx_mock.get_requests(url=URL_HIRE_R1_DETAIL)[0]
    assert json.loads(detail.content) == {
        "orgId": "tesla", "jobId": TESLA_JOB_ID, "siteId": 100004142,
    }
    assert detail.headers["referer"] == (
        "https://hire-r1.mokahr.com/social-recruitment/tesla/100004142"
    )


def test_an_encrypted_posting_is_read(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_APP, json=envelope([TESLA_ROW]))
    httpx_mock.add_response(url=URL_APP_DETAIL, json=_encrypted(TESLA_POSTING))

    [result] = MokaScraper("tesla/100004142").fetch()

    assert result.description == TESLA_TEXT


def test_a_closed_posting_leaves_the_row_as_listed(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_APP, json=envelope([TESLA_ROW]))
    httpx_mock.add_response(
        url=URL_APP_DETAIL,
        json=_encrypted({
            "code": 703015, "codeType": 0, "success": False,
            "msg": "您访问的职位已不存在，请浏览其他职位",
        }),
    )

    [result] = MokaScraper("tesla/100004142").fetch()

    assert result.description is None
    assert result.location is None


def test_get_description_reads_the_posting_and_its_places(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_HIRE_R1, json=envelope([TESLA_ROW]))
    scraper = MokaScraper("hire-r1/tesla/100004142", include_descriptions=False)
    [result] = scraper.fetch()
    assert result.description is None

    httpx_mock.add_response(url=URL_HIRE_R1_DETAIL, json=TESLA_POSTING)

    assert scraper.get_description(result) == TESLA_TEXT
    assert result.location == "Pudong, Shanghai, China; Chaoyang, Beijing, China"


def test_a_listed_place_is_kept_over_the_postings(httpx_mock) -> None:
    row = {**TESLA_ROW, "locations": [{"cityName": "上海市", "provinceName": "上海", "country": "中国"}]}
    httpx_mock.add_response(url=URL_HIRE_R1, json=envelope([row]))
    httpx_mock.add_response(url=URL_HIRE_R1_DETAIL, json=TESLA_POSTING)

    [result] = MokaScraper("hire-r1/tesla/100004142").fetch()

    assert result.location == "上海市, 上海, 中国"
    assert result.description == TESLA_TEXT


def test_get_description_moves_past_a_closed_posting(httpx_mock) -> None:
    other_id = "6a8dfcf9-5bef-4665-810e-15918bf6c4fd"
    rows = [TESLA_ROW, {**TESLA_ROW, "id": other_id}]
    httpx_mock.add_response(url=URL_HIRE_R1, json=envelope(rows))
    scraper = MokaScraper("hire-r1/tesla/100004142", include_descriptions=False)
    closed, open_ = scraper.fetch()
    httpx_mock.add_response(
        url=URL_HIRE_R1_DETAIL,
        match_json={"orgId": "tesla", "jobId": TESLA_JOB_ID, "siteId": 100004142},
        json={"code": 703015, "codeType": 0, "success": False,
              "msg": "您访问的职位已不存在，请浏览其他职位"},
    )
    posting = {**TESLA_POSTING, "data": {**TESLA_POSTING["data"], "id": other_id}}
    httpx_mock.add_response(
        url=URL_HIRE_R1_DETAIL,
        match_json={"orgId": "tesla", "jobId": other_id, "siteId": 100004142},
        json=posting,
    )

    scraper.enrich_descriptions([closed, open_])

    assert closed.description is None
    assert open_.description == TESLA_TEXT
