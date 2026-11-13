from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import HrmosScraper
from ats_scrapers.scrapers.base import ScraperRegistry

TENANT = "acme"
LISTING_URL = f"https://hrmos.co/pages/{TENANT}/jobs"


def _card(
    job_id: str,
    *,
    title: str = "Backend Engineer",
    company: str | None = None,
    location: str = "東京都渋谷区",
    commitment: str = "正社員",
    description: str = "Build reliable distributed systems.",
) -> str:
    company_tag = f"<li>{company}</li>" if company else ""
    return f"""
      <li class="pg-list-cassette">
        <a href="https://hrmos.co/pages/{TENANT}/jobs/{job_id}">
          <div class="pg-list-cassette-detail">
            <h2>{title}</h2>
            <span class="pg-list-cassette-body">{description}</span>
          </div>
          <ul class="sg-tags">
            {company_tag}
            <li>Engineering</li>
            <li>{commitment}</li>
            <li class="sg-tag-location">{location}</li>
          </ul>
        </a>
      </li>
    """


def _page(
    cards: list[str],
    *,
    total: int | None = None,
    displayed: int | None = None,
    company: str = "Acme Group すべての求人一覧",
) -> str:
    total = len(cards) if total is None else total
    displayed = len(cards) if displayed is None else displayed
    return f"""
      <html><body>
        <h1 class="sg-corporate-name">{company}</h1>
        <div class="pg-list-wrapper">
          <div>
            <h3 class="jobCategory-title">会社名</h3>
            <div class="sg-filter-button">Acme Japan</div>
          </div>
          <ul>{''.join(cards)}</ul>
          <p class="pg-count">全 {total:,} 件中 {displayed:,} 件 を表示しています</p>
        </div>
      </body></html>
    """


def test_registry_resolves_hrmos() -> None:
    assert ScraperRegistry.get(ATSType.HRMOS) is HrmosScraper


def test_fetches_server_rendered_jobs_with_structured_fields(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_page(
            [
                _card(
                    "B0000012",
                    title="フルリモート Backend Engineer",
                    company="Acme Japan",
                )
            ]
        ),
    )

    job = HrmosScraper(TENANT).fetch()[0]

    assert job.ats_type is ATSType.HRMOS
    assert job.ats_id == "acme:B0000012"
    assert job.title == "フルリモート Backend Engineer"
    assert job.company == "Acme Japan"
    assert str(job.url) == "https://hrmos.co/pages/acme/jobs/B0000012"
    assert str(job.apply_url) == "https://hrmos.co/pages/acme/jobs/B0000012/apply"
    assert job.location == "東京都渋谷区"
    assert job.country_iso == "JP"
    assert job.region == "Asia"
    assert job.is_remote is True
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "正社員"
    assert job.description == "Build reliable distributed systems."
    assert job.language == "ja"
    assert job.raw == {"tags": ["Engineering"]}


def test_listing_only_mode_omits_description(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_page([_card("1")]))

    job = HrmosScraper(TENANT, include_descriptions=False).fetch()[0]

    assert job.description is None
    assert job.ats_id == "acme:1"


def test_default_company_strips_hrmos_heading_suffix(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_page([_card("1")]))

    job = HrmosScraper(TENANT).fetch()[0]

    assert job.company == "Acme Group"


def test_paginates_until_advertised_total(httpx_mock) -> None:
    first_cards = [_card(str(index)) for index in range(100)]
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_page(first_cards, total=101),
    )
    httpx_mock.add_response(
        url=f"{LISTING_URL}?page=2",
        text=_page([_card("100")], total=101),
    )

    jobs = HrmosScraper(TENANT, include_descriptions=False).fetch()

    assert len(jobs) == 101
    assert jobs[-1].ats_id == "acme:100"


def test_total_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_page([_card("1")], total=2),
    )

    with pytest.raises(ScraperError, match="expected 2 jobs"):
        HrmosScraper(TENANT).fetch()


def test_page_metadata_change_fails_closed(httpx_mock) -> None:
    first_cards = [_card(str(index)) for index in range(100)]
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_page(first_cards, total=101),
    )
    httpx_mock.add_response(
        url=f"{LISTING_URL}?page=2",
        text=_page([_card("100")], total=102),
    )

    with pytest.raises(ScraperError, match="changed while paginating"):
        HrmosScraper(TENANT).fetch()


def test_duplicate_job_ids_fail_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_page([_card("1"), _card("1")]),
    )

    with pytest.raises(ScraperError, match="duplicate job IDs"):
        HrmosScraper(TENANT).fetch()


def test_advertised_page_count_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_page([_card("1")], displayed=2),
    )

    with pytest.raises(ScraperError, match="advertised 2 jobs"):
        HrmosScraper(TENANT).fetch()


def test_unrecognized_html_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text="<html><body>maintenance</body></html>",
    )

    with pytest.raises(ScraperError, match="unrecognized careers page"):
        HrmosScraper(TENANT).fetch()


def test_untrusted_job_url_fails_closed(httpx_mock) -> None:
    bad = _card("1").replace(
        "https://hrmos.co/pages/acme/jobs/1",
        "https://evil.example/pages/acme/jobs/1",
    )
    httpx_mock.add_response(url=LISTING_URL, text=_page([bad]))

    with pytest.raises(ScraperError, match="untrusted job URL"):
        HrmosScraper(TENANT).fetch()


def test_404_maps_to_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        HrmosScraper(TENANT).fetch()


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "bad_slug",
        "../acme",
        "https://hrmos.co/pages/acme/jobs",
        "acme.example",
    ],
)
def test_rejects_untrusted_tenant_identifiers(slug: str) -> None:
    with pytest.raises(ScraperError, match="HrmosScraper"):
        HrmosScraper(slug)
