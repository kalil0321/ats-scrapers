from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import AppliTrackScraper
from ats_scrapers.scrapers.base import ScraperRegistry

TENANT = "leander"
LISTING_URL = (
    "https://www.applitrack.com/leander/onlineapp/"
    "JobPostings/Output.asp?all=1"
)


def _posting(
    *,
    job_id: str = "10217",
    district_id: str = "",
    title: str = "Elementary Teacher",
    district: str | None = None,
    board: str | None = "Leander Independent School District",
    description: str = "<p>Teach &amp; support students.</p>",
) -> str:
    district_html = (
        "<li><span class='label'>District:</span><br/>"
        f"<span class='normal'>{district}</span></li>"
        if district
        else ""
    )
    email_html = (
        "I thought you would be interested in an employment "
        "opportunity I found at "
        f"{board}. The position is {title}."
        if board
        else ""
    )
    return (
        f"<ul class='postingsList' id='p{job_id}_{district_id}'>"
        "<table class='title'><tr>"
        f"<td id='wrapword'>{title}</td>"
        f"<td>JobID: {job_id}</td>"
        "</tr></table>"
        "<li><span class='label'>Position Type:</span><br/>"
        "<span class='normal'>Teaching / Elementary</span></li>"
        "<li><span class='label'>Date Posted:</span><br/>"
        "<span class='normal'>7/29/2026</span></li>"
        "<li><span class='label'>Location:</span><br/>"
        "<span class='normal'>Leander High School</span></li>"
        "<li><span class='label'>Date Available:</span><br/>"
        "<span class='normal'>8/10/2026</span></li>"
        "<li><span class='label'>Closing Date:</span><br/>"
        "<span class='normal'>Open until filled</span></li>"
        f"{district_html}"
        "<table><tr>"
        "<td class='label'>Salary Range:</td>"
        "<td class='label'>Full/Part-Time:</td>"
        "<td class='label'>Work Days/Year:</td>"
        "</tr><tr>"
        "<td><span class='normal'>$65,000</span></td>"
        "<td><span class='normal'>Full-Time</span></td>"
        "<td><span class='normal'>187 days</span></td>"
        "</tr></table>"
        f"<span id='DescriptionText{job_id}_{district_id}'>"
        f"<span class='normal'>{description}</span>"
        "<br/><img src='https://www.applitrack.com/clear.gif'>"
        f"{email_html}"
        "</ul>"
    )


def _payload(*postings: str, advertised: int | None = None) -> str:
    count = len(postings) if advertised is None else advertised
    return (
        "var VacanciesAreOnThisPage = true;"
        "document.write('<div id=\"AppliTrackOutput\">"
        f"Viewing All Types&nbsp;(<b>{count}</b> openings)"
        f"{''.join(postings)}</div>');"
    )


def test_registry_resolves_applitrack() -> None:
    assert ScraperRegistry.get(ATSType.APPLITRACK) is AppliTrackScraper


def test_fetches_structured_public_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_payload(_posting()))

    job = AppliTrackScraper(TENANT, country_iso="us").fetch()[0]

    assert job.ats_type is ATSType.APPLITRACK
    assert job.ats_id == "leander:10217"
    assert job.title == "Elementary Teacher"
    assert job.company == "Leander Independent School District"
    assert str(job.url) == (
        "https://www.applitrack.com/leander/onlineapp/"
        "JobPostings/view.asp?AppliTrackJobId=10217"
        "&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1"
    )
    assert str(job.apply_url) == str(job.url)
    assert job.location == "Leander High School"
    assert job.country_iso == "US"
    assert job.region == "North America"
    assert job.salary_summary == "$65,000"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Full-Time"
    assert job.department == "Teaching / Elementary"
    assert job.description == "Teach & support students."
    assert job.posted_at == datetime(2026, 7, 29, tzinfo=UTC)
    assert job.raw == {
        "source_tenant": "leander",
        "position_type": "Teaching / Elementary",
        "date_available": "8/10/2026",
        "closing_date": "Open until filled",
        "work_days_per_year": "187 days",
    }


def test_consortium_ids_include_district_and_preserve_employer(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_payload(
            _posting(
                job_id="474",
                district_id="142",
                title="Track Coach",
                district="South Redford School District",
            ),
            _posting(
                job_id="474",
                district_id="55177",
                title="Elementary Teacher",
                district="Hamtramck Public Schools",
            ),
        ),
    )

    jobs = AppliTrackScraper(TENANT).fetch()

    assert [job.ats_id for job in jobs] == ["142:474", "55177:474"]
    assert [job.company for job in jobs] == [
        "South Redford School District",
        "Hamtramck Public Schools",
    ]
    assert "AppliTrackJobId=474_142" in str(jobs[0].url)
    assert "AppliTrackJobId=474_55177" in str(jobs[1].url)


def test_explicit_company_is_used_when_job_omits_employer(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_payload(_posting(board=None)),
    )

    job = AppliTrackScraper(
        TENANT,
        company_name="Leander ISD",
    ).fetch()[0]

    assert job.company == "Leander ISD"


def test_listing_only_mode_omits_description(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_payload(_posting()))

    job = AppliTrackScraper(
        TENANT,
        include_descriptions=False,
    ).fetch()[0]

    assert job.description is None


def test_empty_board_returns_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=(
            "var VacanciesAreOnThisPage = true;"
            "document.write('<div id=\"AppliTrackOutput\">"
            "&nbsp;(no results)</div>');"
        ),
    )

    assert AppliTrackScraper(TENANT).fetch() == []


def test_advertised_count_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_payload(_posting(), advertised=2),
    )

    with pytest.raises(ScraperError, match="advertised 2 openings"):
        AppliTrackScraper(TENANT).fetch()


def test_unrecognized_response_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text="<html>maintenance</html>")

    with pytest.raises(ScraperError, match="omitted AppliTrackOutput"):
        AppliTrackScraper(TENANT).fetch()


def test_missing_title_fails_closed(httpx_mock) -> None:
    posting = _posting().replace(
        "<td id='wrapword'>Elementary Teacher</td>",
        "",
    )
    httpx_mock.add_response(url=LISTING_URL, text=_payload(posting))

    with pytest.raises(ScraperError, match="omitted its title"):
        AppliTrackScraper(TENANT).fetch()


def test_identical_duplicate_composite_ids_are_collapsed(httpx_mock) -> None:
    posting = _posting(job_id="123", district_id="456")
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_payload(posting, posting),
    )

    jobs = AppliTrackScraper(TENANT).fetch()

    assert len(jobs) == 1
    assert jobs[0].ats_id == "456:123"


def test_duplicate_id_keeps_available_position_type(httpx_mock) -> None:
    posting = _posting(job_id="123", district_id="456")
    uncategorized = posting.replace(
        "<span class='normal'>Teaching / Elementary</span>",
        "<span class='normal'></span>",
    )
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_payload(uncategorized, posting),
    )

    job = AppliTrackScraper(TENANT).fetch()[0]

    assert job.department == "Teaching / Elementary"
    assert job.raw is not None
    assert job.raw["position_type"] == "Teaching / Elementary"


def test_conflicting_duplicate_id_fails_closed(httpx_mock) -> None:
    posting = _posting(job_id="123", district_id="456")
    conflict = _posting(
        job_id="123",
        district_id="456",
        title="Different Job",
    )
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_payload(posting, conflict),
    )

    with pytest.raises(ScraperError, match="conflicting duplicate"):
        AppliTrackScraper(TENANT).fetch()


def test_full_public_url_is_normalized() -> None:
    scraper = AppliTrackScraper(
        "https://phl.applitrack.com/RESA/onlineapp/"
        "JobPostings/view.asp"
    )

    assert scraper.host == "phl.applitrack.com"
    assert scraper.tenant == "resa"
    assert scraper.listing_url == (
        "https://phl.applitrack.com/resa/onlineapp/"
        "JobPostings/Output.asp?all=1"
    )


def test_404_maps_to_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        AppliTrackScraper(TENANT).fetch()


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "bad_tenant",
        "-leading",
        "trailing-",
        "https://evil.example/leander/onlineapp",
        "http://www.applitrack.com/leander/onlineapp",
        "https://www.applitrack.com/leander/onlineapp?next=evil",
        "https://www.applitrack.com/leander/jobs",
        "https://www.applitrack.com.evil.example/leander/onlineapp",
    ],
)
def test_rejects_untrusted_tenants(slug: str) -> None:
    with pytest.raises(ScraperError, match="AppliTrackScraper"):
        AppliTrackScraper(slug)


def test_rejects_invalid_country_code() -> None:
    with pytest.raises(ScraperError, match="invalid country code"):
        AppliTrackScraper(TENANT, country_iso="USA")
