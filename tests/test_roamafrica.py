"""Tests for the ROAM Africa multi-country scraper.

ROAM Africa runs a shared Laravel/Tailwind template across five
country sites (Jobberman NG/GH, BrighterMonday KE/UG/TZ). Tests pin:

- Region selection via ``company_slug`` → base URL, country_iso,
  currency, language.
- Listing-card HTML parsing for title / url / company / location /
  employment_type / salary / posted_at.
- Pagination cutoff (2 consecutive empty pages stops the crawl).
- JSON-LD ``JobPosting`` detail-page helper.
- Registry wiring.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import RoamAfricaScraper, ScraperRegistry
from jobhive.scrapers.roamafrica import REGIONS


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.roamafrica as ra
    monkeypatch.setattr(ra, "MAX_RETRIES", 1)
    monkeypatch.setattr(ra, "RETRY_BASE_DELAY", 0.0)


# A single realistic ROAM Africa job card mirroring the live HTML
# structure (data-cy markers, badge classes, ``NGN <span>`` salary
# chip, "N days ago" timer). One card = one regex hit on _CARD_RE.
#
# ``featured`` toggles the attribute ordering on the title anchor:
# real ROAM cards put ``href`` before ``data-cy`` on featured cards
# and the reverse on regular ones, so the parser has to handle both.
def _card(
    *,
    job_id: int,
    title: str,
    slug: str,
    base: str = "https://www.jobberman.com",
    company: str = "Acme Co",
    location: str = "Lagos & Lagos State",
    emp_label: str = "Full Time",
    currency: str = "NGN",
    salary: str = "150,000 - 250,000",
    time_ago: str = "3 days ago",
    featured: bool = False,
) -> str:
    if featured:
        anchor = f"""
        <a
           href="{base}/listings/{slug}"
           class="block text-link-500"
           data-cy="listing-title-link"
           title="{title}">
          <p class="text-lg font-medium break-words text-link-500">{title}</p>
        </a>
        """
    else:
        anchor = f"""
        <a data-cy="listing-title-link"
           href="{base}/listings/{slug}"
           class="block text-link-500"
           title="{title}">
          <p class="text-lg font-medium break-words text-link-500">{title}</p>
        </a>
        """
    return f"""
    <div data-cy="listing-cards-components" aria-labelledby="job-{job_id}-title">
      <div class="flex-1">
        {anchor}
        <p class="text-sm text-blue-700 inline-block mt-3">
          {company}
        </p>
        <div class="flex flex-wrap mt-3 text-sm text-gray-500 md:py-0">
          <span class="mb-3 px-3 py-1 rounded bg-brand-secondary-100 mr-2 text-gray-700">
            {location}
          </span>
          <span class="mb-3 px-3 py-1 rounded bg-brand-secondary-100 mr-2 text-gray-700">{emp_label}</span>
          <span class="mb-3 px-3 py-1 rounded bg-brand-secondary-100 mr-2 text-gray-700">
            {currency} <span class="mr-1">{salary}</span>
          </span>
        </div>
      </div>
      <div class="ml-auto flex items-center gap-3">
        <p class="text-sm font-normal text-gray-700">{time_ago}</p>
      </div>
    </div>
    """


def _listing_page(cards: list[str]) -> str:
    """Wrap one or more cards in a minimal page shell."""
    body = "\n".join(cards)
    return f"""
    <!DOCTYPE html>
    <html lang="en-ng">
      <head><title>Jobs</title></head>
      <body>
        <main>
          {body}
        </main>
        <footer>x</footer>
      </body>
    </html>
    """


def _empty_page() -> str:
    return _listing_page([])


# --- registry / region wiring -----------------------------------------------


def test_registry_resolves_roamafrica() -> None:
    assert ScraperRegistry.get(ATSType.ROAMAFRICA) is RoamAfricaScraper


@pytest.mark.parametrize("slug", list(REGIONS))
def test_supported_regions_construct(slug: str) -> None:
    s = RoamAfricaScraper(slug)
    base, country, lang = REGIONS[slug]
    assert s._base_url == base
    assert s._country_iso == country
    assert s._language == lang


def test_unknown_region_rejected() -> None:
    with pytest.raises(ScraperError):
        RoamAfricaScraper("jobberman-zw")


def test_region_currency_per_country() -> None:
    """Each region maps to its national currency (NGN/KES/UGX/TZS/GHS)."""
    expected = {
        "jobberman-ng": "NGN",
        "brightermonday-ke": "KES",
        "brightermonday-ug": "UGX",
        "brightermonday-tz": "TZS",
        "jobberman-gh": "GHS",
    }
    for slug, currency in expected.items():
        assert RoamAfricaScraper(slug)._currency == currency


# --- happy path -------------------------------------------------------------


def test_parses_listing_card_full_fields(httpx_mock) -> None:
    """A realistic Jobberman NG card produces a complete Job row."""
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1",
        text=_listing_page([
            _card(
                job_id=1226658,
                title="Bar/Lounge Supervisor",
                slug="barlounge-supervisor-k77808",
                company="KeiOyi Investments Limited",
                location="Port Harcourt & Rivers State",
                emp_label="Full Time",
                currency="NGN",
                salary="70,000 - 150,000",
                time_ago="3 days ago",
            ),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )

    jobs = RoamAfricaScraper("jobberman-ng").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.ROAMAFRICA
    assert j.ats_id == "1226658"
    assert j.title == "Bar/Lounge Supervisor"
    assert j.company == "KeiOyi Investments Limited"
    assert (
        str(j.url)
        == "https://www.jobberman.com/listings/barlounge-supervisor-k77808"
    )
    assert j.location == "Port Harcourt & Rivers State"
    assert j.country_iso == "NG"
    assert j.region == "Africa"
    assert j.language == "en"
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Full Time"
    assert j.salary_currency == "NGN"
    assert j.salary_min == 70_000
    assert j.salary_max == 150_000
    assert j.salary_period == "MONTH"
    assert j.salary_summary == "NGN 70,000 - 150,000"
    assert j.posted_at is not None
    # Approximate by design — within 1 hour of now-3-days.
    assert datetime.now() - timedelta(days=3, hours=1) < j.posted_at
    assert j.posted_at < datetime.now()
    assert j.raw == {
        "region_key": "jobberman-ng",
        "posted_text": "3 days ago",
    }
    assert j.global_id == "roamafrica:1226658"


def test_parses_brightermonday_ke_with_kes_currency(httpx_mock) -> None:
    """Switching region changes base URL, country code, currency."""
    httpx_mock.add_response(
        url="https://www.brightermonday.co.ke/jobs?page=1",
        text=_listing_page([
            _card(
                job_id=1174836,
                title="Senior Backend Engineer",
                slug="senior-backend-engineer-abcd1",
                base="https://www.brightermonday.co.ke",
                company="Safaricom",
                location="Nairobi",
                emp_label="Full Time",
                currency="KES",
                salary="200,000 - 400,000",
            ),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.brightermonday\.co\.ke/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )

    jobs = RoamAfricaScraper("brightermonday-ke").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.country_iso == "KE"
    assert j.salary_currency == "KES"
    assert str(j.url).startswith("https://www.brightermonday.co.ke/listings/")


def test_featured_card_anchor_attribute_order(httpx_mock) -> None:
    """Featured listings put ``href`` BEFORE ``data-cy`` on the title
    anchor; regular cards do the reverse. Parser must handle both."""
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1",
        text=_listing_page([
            _card(
                job_id=42, title="Featured Role",
                slug="featured-role-42", featured=True,
            ),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = RoamAfricaScraper("jobberman-ng").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Featured Role"
    assert str(jobs[0].url) == "https://www.jobberman.com/listings/featured-role-42"


def test_handles_commission_only_salary_chip(httpx_mock) -> None:
    """Non-numeric salary strings (Commission Only / Confidential) keep
    summary + currency but min/max stay None."""
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1",
        text=_listing_page([
            _card(
                job_id=999,
                title="Sales Rep",
                slug="sales-rep-xxx",
                currency="NGN",
                salary="Commission Only",
            ),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = RoamAfricaScraper("jobberman-ng").fetch()[0]
    assert j.salary_currency == "NGN"
    assert j.salary_summary == "NGN Commission Only"
    assert j.salary_min is None
    assert j.salary_max is None


def test_employment_type_label_variants_map_to_enum(httpx_mock) -> None:
    """All five badge spellings normalise to the canonical enum."""
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1",
        text=_listing_page([
            _card(job_id=1, title="A", slug="a-1", emp_label="Full Time"),
            _card(job_id=2, title="B", slug="b-2", emp_label="Part Time"),
            _card(job_id=3, title="C", slug="c-3", emp_label="Contract"),
            _card(job_id=4, title="D", slug="d-4", emp_label="Internship"),
            _card(job_id=5, title="E", slug="e-5", emp_label="Temporary"),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = RoamAfricaScraper("jobberman-ng").fetch()
    et = {j.ats_id: j.employment_type for j in jobs}
    assert et == {
        "1": "FULL_TIME",
        "2": "PART_TIME",
        "3": "CONTRACT",
        "4": "INTERN",
        "5": "TEMPORARY",
    }


def test_dedup_across_pages_by_listing_id(httpx_mock) -> None:
    """Featured listings repeat on every page — dedup keeps each
    listing id exactly once across the whole crawl."""
    page1 = _listing_page([
        _card(job_id=100, title="A", slug="a-100"),
        _card(job_id=200, title="B", slug="b-200"),
    ])
    page2 = _listing_page([
        _card(job_id=100, title="A", slug="a-100"),  # repeat
        _card(job_id=300, title="C", slug="c-300"),
    ])
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1", text=page1,
    )
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=2", text=page2,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[3-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = RoamAfricaScraper("jobberman-ng").fetch()
    assert sorted(j.ats_id for j in jobs) == ["100", "200", "300"]


# --- pagination -------------------------------------------------------------


def test_stops_after_two_consecutive_empty_pages(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1",
        text=_listing_page([_card(job_id=1, title="A", slug="a-1")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = RoamAfricaScraper("jobberman-ng", max_pages=20).fetch()
    assert {j.ats_id for j in jobs} == {"1"}


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even with all-fresh pages, max_pages bounds the crawl."""
    for p in range(1, 6):
        httpx_mock.add_response(
            url=f"https://www.jobberman.com/jobs?page={p}",
            text=_listing_page([
                _card(job_id=p * 1000, title=f"Job {p}", slug=f"j-{p}"),
            ]),
        )
    jobs = RoamAfricaScraper("jobberman-ng", max_pages=5).fetch()
    assert len(jobs) == 5


def test_404_treated_as_end_of_pagination(httpx_mock) -> None:
    """A 404 on page N stops the crawl gracefully, keeping prior pages."""
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1",
        text=_listing_page([_card(job_id=1, title="A", slug="a-1")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[2-9]$"),
        status_code=404, is_reusable=True,
    )
    jobs = RoamAfricaScraper("jobberman-ng").fetch()
    assert len(jobs) == 1


# --- detail-page JSON-LD ----------------------------------------------------


def test_parse_detail_jsonld_extracts_jobposting() -> None:
    """The static helper finds the JobPosting node inside the page's
    ``@graph`` and returns its raw dict for downstream enrichment."""
    detail_html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@graph": [
          {"@type": "WebPage", "name": "x"},
          {
            "@type": "JobPosting",
            "title": "Bar/Lounge Supervisor",
            "description": "<p>Run the floor.</p>",
            "datePosted": "2026-05-11T00:00:00.000000Z",
            "employmentType": "FULL_TIME",
            "industry": "Hospitality",
            "baseSalary": {
              "@type": "MonetaryAmount",
              "currency": "NGN",
              "value": {"@type": "QuantitativeValue",
                        "minValue": 70000, "maxValue": 150000,
                        "unitText": "MONTH"}
            }
          }
        ]
      }
      </script>
    </head><body></body></html>
    """
    node = RoamAfricaScraper.parse_detail_jsonld(detail_html)
    assert node is not None
    assert node["title"] == "Bar/Lounge Supervisor"
    assert node["employmentType"] == "FULL_TIME"
    assert node["baseSalary"]["currency"] == "NGN"


def test_parse_detail_jsonld_returns_none_when_absent() -> None:
    assert RoamAfricaScraper.parse_detail_jsonld("<html></html>") is None


# --- error handling ---------------------------------------------------------


def test_persistent_500_on_page_one_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=\d+$"),
        status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        RoamAfricaScraper("jobberman-ng").fetch()


def test_500_mid_crawl_keeps_collected_jobs(httpx_mock) -> None:
    """If a later page 500s, the scraper logs and returns the page-1
    jobs rather than throwing them away."""
    httpx_mock.add_response(
        url="https://www.jobberman.com/jobs?page=1",
        text=_listing_page([_card(job_id=1, title="A", slug="a-1")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobberman\.com/jobs\?page=[2-9]$"),
        status_code=500, is_reusable=True,
    )
    jobs = RoamAfricaScraper("jobberman-ng").fetch()
    assert len(jobs) == 1
