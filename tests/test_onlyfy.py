from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.resolve import resolve_careers_url
from ats_scrapers.scrapers import OnlyfyScraper, onlyfy
from ats_scrapers.scrapers.base import ScraperRegistry

SITE = "https://acme.onlyfy.jobs"


def _card(job_id: str, title: str, info: str, *, locale: str = "en", query: str = "") -> str:
    return (
        f'<li><a data-testid="job-card" aria-label="{title}" href="/{locale}/job/{job_id}{query}">'
        f'<h3 data-testid="job-title">{title}</h3>'
        f'<div data-testid="job-more-info">{info}</div></a></li>'
    )


def _page(cards: list[str], *, count: str | None = None, found: str | None = None) -> str:
    pager = f'<span data-testid="pagination-items-count">{count}</span>' if count else ""
    body = (
        f'<ul data-testid="jobs-list">{"".join(cards)}</ul>{pager}'
        if cards
        else '<div data-testid="jobs-empty-state"></div>'
    )
    if found is None:
        found = f"{len(cards)} jobs found" if cards else "No job openings right now"
    return (
        "<html><head><title>Acme GmbH &amp; Co. KG</title></head><body>"
        f'<p data-testid="jobs-results-announcement">{found}</p>'
        f'<div data-testid="jobs-content">{body}</div></body></html>'
    )


def _posting(**fields: object) -> str:
    posting = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Mechatroniker",
        "description": "Wir suchen:<h1>Aufgaben</h1><p>Wartung der Anlagen.</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Acme Holdings GmbH"},
        "datePosted": "2026-10-22",
        "employmentType": "FULL_TIME",
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Renningen",
                "addressCountry": "DE",
                "postalCode": "71272",
            },
        },
        **fields,
    }
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(posting)}</script></head><body></body></html>"
    )


def test_registry_and_address_resolve_onlyfy() -> None:
    assert ScraperRegistry.get(ATSType.ONLYFY) is OnlyfyScraper
    assert resolve_careers_url("https://www.onlyfy.jobs/") is None
    resolved = resolve_careers_url("https://acme.onlyfy.jobs/en/job/abc123")
    assert resolved is not None
    assert (resolved.ats, resolved.slug) == (ATSType.ONLYFY, "acme")


def test_reads_a_company_from_its_list(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page([
        _card("abc1", "Mechatroniker (m/w/d)", "Renningen | Full-time employee | 22.10.2026 | Stuttgart"),
        _card("abc2", "Azubi Lager", "Gescher | Apprenticeship | 06.08.2026"),
    ]))

    first, second = OnlyfyScraper("Acme", include_descriptions=False).fetch()

    assert first.global_id == "onlyfy:abc1"
    assert str(first.url) == f"{SITE}/en/job/abc1"
    assert (first.title, first.company) == ("Mechatroniker (m/w/d)", "Acme GmbH & Co. KG")
    assert (first.location, first.commitment) == ("Renningen", "Full-time employee")
    assert (first.employment_type, first.engagement_type) == ("FULL_TIME", None)
    assert first.posted_at == datetime(2026, 10, 22, tzinfo=UTC)
    assert first.description is None
    assert (second.employment_type, second.engagement_type) == (None, "apprenticeship")
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize(
    ("info", "employment", "engagement", "location"),
    [
        ("Wien | Part-time employee | 01.02.2026", "PART_TIME", None, "Wien"),
        ("Linz | Internship | 01.02.2026", "INTERN", "internship", "Linz"),
        ("Graz | Werkstudent | 01.02.2026", "INTERN", "internship", "Graz"),
        ("Bonn | Freelancer | 01.02.2026", "CONTRACT", "independent_contractor", "Bonn"),
        ("Köln | Vollzeit | 31.02.2026", "FULL_TIME", None, "Köln"),
        ("Ulm | Unbefristet | 01.02.2026", None, "permanent", "Ulm"),
        ("Ulm | Vollzeit, unbefristet | 01.02.2026", "FULL_TIME", "permanent", "Ulm"),
        ("Ulm | Teilzeit, befristet | 01.02.2026", "PART_TIME", "temporary", "Ulm"),
        ("Kiel | Befristet/Saisonal | 01.02.2026", "TEMPORARY", "temporary", "Kiel"),
        ("Kiel | Stand-in / temporary | 01.02.2026", "TEMPORARY", "temporary", "Kiel"),
        ("Bern | Part-time / full-time | 01.02.2026", None, None, "Bern"),
        ("Bern | Marginal employment | 01.02.2026", None, None, "Bern"),
        ("Bonn | Internationales Marketing | 01.02.2026", None, None, "Bonn"),
        ("Bonn | Aus- und Weiterbildung | 01.02.2026", None, None, "Bonn"),
        ("Remote", None, None, "Remote"),
        ("", None, None, None),
    ],
)
def test_reads_what_a_card_prints(
    httpx_mock, info: str, employment: str | None, engagement: str | None, location: str | None,
) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page([_card("abc1", "Stelle", info)]))

    job = OnlyfyScraper("acme", include_descriptions=False).fetch()[0]

    assert (job.employment_type, job.engagement_type, job.location) == (
        employment, engagement, location,
    )


def test_turns_every_page_and_keeps_a_job_once(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(onlyfy, "PAGE_SIZE", 2)
    httpx_mock.add_response(url=f"{SITE}/en", text=_page(
        [_card("a1", "A", ""), _card("a2", "B", "")],
        count="<b>1-2</b> out of <b>3 jobs</b>", found="3 jobs found",
    ))
    httpx_mock.add_response(url=f"{SITE}/en?page=2", text=_page([
        _card("a2", "B", "", query="?page=2"), _card("a3", "C", "", query="?page=2"),
    ], found="3 jobs found"))

    jobs = OnlyfyScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["a1", "a2", "a3"]
    assert str(jobs[-1].url) == f"{SITE}/en/job/a3"
    assert jobs[0].commitment is None


def test_a_list_longer_than_the_page_limit_is_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(onlyfy, "MAX_PAGES", 2)
    httpx_mock.add_response(url=f"{SITE}/en", text=_page(
        [_card("a1", "A", "")], count="<b>1-10</b> out of <b>21 jobs</b>",
    ))

    with pytest.raises(ScraperError, match="more than 2 pages"):
        OnlyfyScraper("acme", include_descriptions=False).fetch()


def test_reads_the_one_language_a_company_publishes(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", status_code=307, headers={"location": "/de"})
    httpx_mock.add_response(url=f"{SITE}/de", text=_page([
        _card("a1", "Lagerist", "Ulm | Teilzeit | 03.03.2026", locale="de"),
    ]))

    job = OnlyfyScraper("acme", include_descriptions=False).fetch()[0]

    assert str(job.url) == f"{SITE}/de/job/a1"
    assert (job.employment_type, job.commitment) == ("PART_TIME", "Teilzeit")


@pytest.mark.parametrize("target", ["https://karriere.acme.de/", "https://other.onlyfy.jobs/en"])
def test_a_page_that_moved_to_another_site_is_not_found(httpx_mock, target: str) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", status_code=308, headers={"location": target})

    with pytest.raises(CompanyNotFoundError, match="moved to"):
        OnlyfyScraper("acme").fetch()


@pytest.mark.parametrize("target", ["/en", "/login", "/en/challenge", ""])
def test_an_unexpected_move_is_an_error_not_a_closed_company(httpx_mock, target: str) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", status_code=307, headers={"location": target})

    with pytest.raises(ScraperError, match="unexpected page") as caught:
        OnlyfyScraper("acme").fetch()

    assert not isinstance(caught.value, CompanyNotFoundError)


def test_a_language_page_that_moves_again_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", status_code=307, headers={"location": "/de"})
    httpx_mock.add_response(url=f"{SITE}/de", status_code=307, headers={"location": "/en"})

    with pytest.raises(ScraperError, match="unexpected page") as caught:
        OnlyfyScraper("acme").fetch()

    assert not isinstance(caught.value, CompanyNotFoundError)


def test_a_closed_career_page_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", status_code=404)

    with pytest.raises(CompanyNotFoundError):
        OnlyfyScraper("acme").fetch()


def test_a_company_with_nothing_open_is_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page([], found="No job openings right now"))

    assert OnlyfyScraper("acme").fetch() == []


@pytest.mark.parametrize("asked", ["noindex, follow", "NOINDEX,FOLLOW", " follow , noindex ", "none"])
def test_a_company_that_asks_not_to_be_indexed_lists_no_jobs(httpx_mock, asked: str) -> None:
    page = _page([_card("abc1", "Mechatroniker", "Renningen")]).replace(
        "<head>", f'<head><meta name="robots" content="{asked}" />'
    )
    httpx_mock.add_response(url=f"{SITE}/en", text=page)

    assert OnlyfyScraper("acme").fetch() == []


@pytest.mark.parametrize("asked", ["index, follow", "nofollow", ""])
def test_a_company_that_does_not_ask_is_read(httpx_mock, asked: str) -> None:
    page = _page([_card("abc1", "Mechatroniker", "Renningen")]).replace(
        "<head>", f'<head><meta name="robots" content="{asked}" /><meta name="description" content="noindex" />'
    )
    httpx_mock.add_response(url=f"{SITE}/en", text=page)

    jobs = OnlyfyScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["abc1"]


def test_a_page_without_a_job_list_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text="<html><body>Wartung</body></html>")

    with pytest.raises(ScraperError, match="unrecognized career page"):
        OnlyfyScraper("acme").fetch()


def test_reads_the_description_from_the_job_page(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page([
        _card("a1", "Mechatroniker", "Renningen | Full-time employee | 20.10.2026"),
        _card("a2", "Azubi", ""),
        _card("a3", "Gone", ""),
    ]))
    httpx_mock.add_response(url=f"{SITE}/en/job/a1", text=_posting())
    httpx_mock.add_response(
        url=f"{SITE}/en/job/a2",
        text=_posting(employmentType="INTERN", hiringOrganization=None, jobLocation=None),
    )
    httpx_mock.add_response(url=f"{SITE}/en/job/a3", text="<html><body>No job</body></html>")

    first, second, third = OnlyfyScraper("acme").fetch()

    assert first.description == "Wir suchen:\nAufgaben\nWartung der Anlagen."
    assert first.company == "Acme Holdings GmbH"
    assert (first.location, first.country_iso) == ("Renningen", "DE")
    assert first.posted_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert first.raw == {"postal_code": "71272"}
    assert (second.company, second.employment_type) == ("Acme GmbH & Co. KG", "INTERN")
    assert second.posted_at == datetime(2026, 10, 22, tzinfo=UTC)
    assert (second.location, second.country_iso) == (None, None)
    assert (third.title, third.description) == ("Gone", None)


@pytest.mark.parametrize("slug", ["", "acme.onlyfy.jobs", "https://acme.onlyfy.jobs", "a b"])
def test_rejects_what_is_not_a_company(slug: str) -> None:
    with pytest.raises(ScraperError):
        OnlyfyScraper(slug)


def test_a_list_that_stays_short_is_read_twice_and_then_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(onlyfy, "PAGE_SIZE", 1)
    httpx_mock.add_response(url=f"{SITE}/en", is_reusable=True, text=_page(
        [_card("a1", "A", "")], count="<b>1-1</b> out of <b>3 jobs</b>", found="3 jobs found",
    ))
    httpx_mock.add_response(
        url=f"{SITE}/en?page=2", is_reusable=True, text=_page([_card("a2", "B", "")]),
    )
    httpx_mock.add_response(
        url=f"{SITE}/en?page=3", is_reusable=True, status_code=307, headers={"location": "/en"},
    )

    with pytest.raises(ScraperError, match="listed 2 of the 3 jobs it announces"):
        OnlyfyScraper("acme", include_descriptions=False).fetch()

    assert len(httpx_mock.get_requests()) == 6


def test_a_job_that_moved_between_two_pages_is_found_on_the_second_reading(
    httpx_mock, monkeypatch,
) -> None:
    monkeypatch.setattr(onlyfy, "PAGE_SIZE", 2)
    first = _page(
        [_card("a1", "A", ""), _card("a2", "B", "")],
        count="<b>1-2</b> out of <b>4 jobs</b>", found="4 jobs found",
    )
    httpx_mock.add_response(url=f"{SITE}/en", text=first)
    # A job was published between the two reads: "a2" is listed again and "a3" is skipped.
    httpx_mock.add_response(url=f"{SITE}/en?page=2", text=_page([
        _card("a2", "B", ""), _card("a4", "D", ""),
    ]))
    httpx_mock.add_response(url=f"{SITE}/en", text=first)
    httpx_mock.add_response(url=f"{SITE}/en?page=2", text=_page([
        _card("a3", "C", ""), _card("a4", "D", ""),
    ]))

    jobs = OnlyfyScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["a1", "a2", "a3", "a4"]


def test_an_inner_page_that_is_not_a_job_list_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page(
        [_card("a1", "A", "")], count="<b>1-10</b> out of <b>20 jobs</b>",
    ))
    httpx_mock.add_response(url=f"{SITE}/en?page=2", text="<html><body>Wartung</body></html>")

    with pytest.raises(ScraperError, match="unrecognized page 2 of the list"):
        OnlyfyScraper("acme", include_descriptions=False).fetch()


@pytest.mark.parametrize("target", ["/login", "https://elsewhere.example/check", "/de"])
def test_an_inner_page_that_moves_elsewhere_is_an_error(httpx_mock, target: str) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page(
        [_card("a1", "A", "")], count="<b>1-10</b> out of <b>20 jobs</b>",
    ))
    httpx_mock.add_response(
        url=f"{SITE}/en?page=2", status_code=307, headers={"location": target},
    )

    with pytest.raises(ScraperError, match="unexpected page"):
        OnlyfyScraper("acme", include_descriptions=False).fetch()


@pytest.mark.parametrize(
    ("status", "words"),
    [(429, "returned 429 after"), (503, "returned 503 after"), (403, "returned 403")],
)
def test_a_refused_company_page_is_an_error_not_an_empty_company(
    httpx_mock, status: int, words: str,
) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", status_code=status, is_reusable=True)

    with pytest.raises(ScraperError, match=words) as caught:
        OnlyfyScraper("acme").fetch()

    assert not isinstance(caught.value, CompanyNotFoundError)


@pytest.mark.parametrize(
    ("status", "words"),
    [(429, "returned 429 after"), (500, "returned 500 after"), (403, "returned 403")],
)
def test_a_page_refused_midway_fails_the_whole_list(httpx_mock, status: int, words: str) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page(
        [_card("a1", "A", "")], count="<b>1-10</b> out of <b>30 jobs</b>",
    ))
    httpx_mock.add_response(url=f"{SITE}/en?page=2", status_code=status, is_reusable=True)
    httpx_mock.add_response(
        url=f"{SITE}/en?page=3", text=_page([_card("a3", "C", "")]), is_optional=True,
    )

    with pytest.raises(ScraperError, match=words):
        OnlyfyScraper("acme", include_descriptions=False).fetch()


def test_a_list_page_that_goes_missing_is_an_error_not_a_closed_company(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page(
        [_card("a1", "A", "")], count="<b>1-10</b> out of <b>20 jobs</b>",
    ))
    httpx_mock.add_response(url=f"{SITE}/en?page=2", status_code=404)

    with pytest.raises(ScraperError, match="page 2 of the list returned 404") as caught:
        OnlyfyScraper("acme", include_descriptions=False).fetch()

    assert not isinstance(caught.value, CompanyNotFoundError)


@pytest.mark.parametrize(
    ("status", "words"),
    [(429, "returned 429 after"), (502, "returned 502 after"), (403, "returned 403")],
)
def test_a_refused_job_page_fails_the_read(httpx_mock, status: int, words: str) -> None:
    httpx_mock.add_response(url=f"{SITE}/en", text=_page([_card("a1", "A", "")]))
    httpx_mock.add_response(url=f"{SITE}/en/job/a1", status_code=status, is_reusable=True)

    with pytest.raises(ScraperError, match=words):
        OnlyfyScraper("acme").fetch()


@pytest.mark.parametrize("pager", ["", "<b>1-10</b> out of <b>12 jobs</b>"])
def test_a_first_page_that_announces_jobs_and_lists_none_is_an_error(httpx_mock, pager: str) -> None:
    page = _page([], found="12 jobs found").replace(
        '<div data-testid="jobs-empty-state"></div>',
        f'<span data-testid="pagination-items-count">{pager}</span>' if pager else "",
    )
    httpx_mock.add_response(url=f"{SITE}/en", text=page)

    with pytest.raises(ScraperError, match="announces 12 jobs and lists none"):
        OnlyfyScraper("acme", include_descriptions=False).fetch()