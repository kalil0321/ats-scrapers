from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.resolve import resolve_careers_url
from ats_scrapers.scrapers import KulaScraper
from ats_scrapers.scrapers.base import ScraperRegistry

BOARD = "https://careers.kula.ai/acme"
SF = {"location": "San Francisco, California, United States", "city": "San Francisco",
      "state": "California", "country": "United States", "country_code": "US"}


def _job(job_id: int, **fields: object) -> dict:
    detail = {
        "job_description": "<p>Run <b>sales</b></p>", "workplace": "office",
        "employment_type": "full_time", "ats_department": {"id": 1, "name": "Sales"},
        "offices": [SF],
    }
    detail.update(fields.pop("detail", {}))  # type: ignore[arg-type]
    job = {"id": job_id, "title": f"Job {job_id}", "listed": True, "on_career_page": True,
           "launch_at": "2026-11-13T17:25:55.000Z", "ats_job": detail}
    job.update(fields)
    return job


def _chunks(texts: dict[str, str]) -> str:
    return "\n" + "".join(f"{key}:T{len(text.encode()):x},{text}" for key, text in texts.items())


def _page(jobs: list[dict], *, links: list[int] | None = None, texts: dict[str, str] | None = None,
          name: str = "Acme") -> str:
    payload = '5:["$","div",null,{"jobs":' + json.dumps(jobs) + ',"departments":[]}]'
    payload += _chunks(texts) if texts else "\n"
    half = len(payload) // 2  # a payload arrives in several pushes
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(piece)}])</script>"
        for piece in (payload[:half], payload[half:])
    )
    ids = links if links is not None else [job["id"] for job in jobs]
    cards = "".join(f'<div class="chakra-card"><a href="/acme/{i}-x">Apply Now</a></div>' for i in ids)
    organization = json.dumps({"@type": "Organization", "name": name})
    return (f"<html><title>Careers @ {name}</title>"
            f'<script type="application/ld+json">{organization}</script>{cards}{pushes}</html>')


def test_registry_and_address_resolve_kula() -> None:
    assert ScraperRegistry.get(ATSType.KULA) is KulaScraper
    resolved = resolve_careers_url("https://careers.kula.ai/acme/19162-account-executive")
    assert resolved is not None and (resolved.ats, resolved.slug) == (ATSType.KULA, "acme")


def test_reads_every_job_of_a_board_from_one_page(httpx_mock) -> None:
    two = [
        {"location": "Chennai, Tamil Nadu, India", "country_code": "IN"},
        {"location": "Bengaluru, Karnataka, India", "country_code": "IN"},
    ]
    httpx_mock.add_response(url=BOARD, text=_page([
        _job(1),
        _job(2, detail={"offices": two, "workplace": "remote", "employment_type": "part_time"}),
        _job(3, detail={"offices": [SF, {"location": "London, England, United Kingdom",
                                           "country_code": "GB"}], "workplace": "hybrid"}),
    ]))

    first, second, third = KulaScraper("acme").fetch()

    assert first.global_id == "kula:1"
    assert str(first.url) == f"{BOARD}/1" and str(first.apply_url) == str(first.url)
    assert first.company == "Acme"
    assert first.location == "San Francisco, California, United States"
    assert (first.country_iso, first.workplace_type, first.is_remote) == ("US", "onsite", None)
    assert (first.employment_type, first.department) == ("FULL_TIME", "Sales")
    assert first.description == "Run sales"
    assert first.posted_at == datetime(2026, 11, 13, 17, 25, 55, tzinfo=UTC)
    assert second.location == "Chennai, Tamil Nadu, India; Bengaluru, Karnataka, India"
    assert (second.country_iso, second.workplace_type, second.is_remote) == ("IN", "remote", True)
    assert second.employment_type == "PART_TIME"
    assert (third.country_iso, third.workplace_type) == (None, "hybrid")
    assert len(third.locations or []) == 2


def test_a_long_description_is_read_from_its_text_chunk_by_byte_length(httpx_mock) -> None:
    texts = {"20": "<p>Café – Zürich, 日本語 ✓</p>", "21": "<p>Second</p>"}
    jobs = [_job(1, detail={"job_description": "$20"}), _job(2, detail={"job_description": "$21"}),
            _job(3, detail={"job_description": "$2f"})]  # the last chunk is missing
    httpx_mock.add_response(url=BOARD, text=_page(jobs, texts=texts))

    descriptions = [job.description for job in KulaScraper("acme").fetch()]

    assert descriptions == ["Café – Zürich, 日本語 ✓", "Second", None]


def test_jobs_not_on_the_public_page_are_skipped(httpx_mock) -> None:
    jobs = [_job(1), _job(2, listed=False), _job(3, on_career_page=False), _job(1)]
    httpx_mock.add_response(url=BOARD, text=_page(jobs, links=[1]))

    assert [job.ats_id for job in KulaScraper("acme").fetch()] == ["1"]


def test_a_board_without_jobs_is_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=BOARD, text=_page([]))

    assert KulaScraper("acme").fetch() == []


def test_a_page_whose_cards_and_data_disagree_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=BOARD, text=_page([_job(1), _job(2), _job(3)], links=[1, 2]))

    with pytest.raises(ScraperError, match="links 2 jobs but its data holds 3"):
        KulaScraper("acme").fetch()


def test_an_unknown_company_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=BOARD, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        KulaScraper("acme").fetch()


def test_a_page_that_is_not_a_board_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=BOARD, text="<html><title>Elsewhere</title></html>")

    with pytest.raises(ScraperError, match="unrecognized page"):
        KulaScraper("acme").fetch()


@pytest.mark.parametrize("slug", ["", "a/b", "a.b", "https://evil.example/x", "-x"])
def test_a_slug_that_is_not_a_company_name_is_refused(slug: str) -> None:
    with pytest.raises(ScraperError, match="invalid company"):
        KulaScraper(slug)
