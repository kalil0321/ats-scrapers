from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.resolve import resolve_careers_url
from ats_scrapers.scrapers import PandapeScraper, pandape
from ats_scrapers.scrapers.base import ScraperRegistry

LIST_URL = "https://acme.pandape.infojobs.com.br/Vacancies"
PAGE_URL = "https://acme.pandape.infojobs.com.br/ListVacancies"


def _fact(icon: str, text: str) -> str:
    return (
        '<div class="align-middle mr-20 mb-10">'
        '<div class="icon-container d-inline-block lh-100 align-middle ">'
        f'<i class="icon icon-{icon}"></i></div> {text} </div>'
    )


def _card(job_id: int, title: str, *facts: str, day: str = "19 nov") -> str:
    return (
        f'<a target="_blank" class="card card-vacancy mb-20" href="/Detail/{job_id}">'
        f'<div class="card-body p-20"><h3 class="link" title="{title}">{title}</h3>'
        f'<div class="vacancy-detail"><div class="d-flex">{"".join(facts)}</div>'
        f'<div class="vacancy-date font-sm">{day}</div></div></div></a>'
    )


def _list(cards: list[str], *, last: bool = True, heading: str | None = "Acme Brasil") -> str:
    name = f"<h1>{heading}</h1>" if heading else ""
    return (
        "<html><head><title>Acme Brasil - Trabalhe Conosco | Pandapé</title></head><body>"
        f'{name}<input type="hidden" id="hdn_isLast" name="IsLast" value="{last}" />'
        '<input type="hidden" id="hdn_PageSize" name="PageSize" value="20" />'
        '<input type="hidden" id="hdn_PageNumber" name="PageNumber" value="1" />'
        f'<div id="VacancyList">{"".join(cards)}</div></body></html>'
    )


def _more(cards: list[str], *, last: bool, total: int) -> dict[str, object]:
    return {"view": "".join(cards), "counter": "\n", "isLast": last, "subtotal": total}


def _page(number: int, size: int = 20) -> str:
    return f"{PAGE_URL}?PageNumber={number}&PageSize={size}"


def _posting(**fields: object) -> str:
    posting = {
        "@context": "http://schema.org",
        "@type": "JobPosting",
        "employmentType": "full-time",
        "title": "Vendedor",
        "description": "Atender clientes.&nbsp;\nOrganizar a loja.",
        "datePosted": "2026-11-12T12:36:10.9982421",
        "hiringOrganization": {"@type": "Organization", "name": "Acme Varejo"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressCountry": "BR",
                "addressLocality": "Uberaba",
                "addressRegion": "MG",
            },
        },
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "BRL",
            "value": {
                "@type": "QuantitativeValue",
                "unitText": "MONTH",
                "minValue": 2000.0,
                "maxValue": 2200.0,
            },
        },
        **fields,
    }
    return (
        "<html><head>"
        f'<script type="application/ld+json" >{json.dumps(posting)}</script>'
        "</head><body></body></html>"
    )


def test_registry_and_address_resolve_pandape() -> None:
    assert ScraperRegistry.get(ATSType.PANDAPE) is PandapeScraper
    assert resolve_careers_url("https://empregos.pandape.infojobs.com.br/Detail/7") is None
    resolved = resolve_careers_url("https://acme.pandape.infojobs.com.br/Detail/7")
    assert resolved is not None
    assert (resolved.ats, resolved.slug) == (ATSType.PANDAPE, "acme")


def test_reads_a_company_from_its_list(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([
        _card(
            7, "Vendedor",
            _fact("location-pin-1", "Uberaba - MG"),
            _fact("buildings", "Presencial"),
            _fact("clock", "Per&#xED;odo Integral"),
            _fact("candidates", "3 Posições"),
            _fact("sheet", "Efetivo &#x2013; CLT"),
            _fact("wallet", "R$ 2.000,00 - R$ 2.200,00"),
        ),
        _card(
            8, "Estagiário de TI",
            _fact("location-pin-1", "S&#xE3;o Paulo - SP"),
            _fact("building-house", "H&#xED;brido"),
            _fact("clock", "Parcial manhãs"),
            _fact("candidates", "1 Posição"),
            _fact("sheet", "Estágio"),
        ),
    ]))

    seller, intern = PandapeScraper("Acme", include_descriptions=False).fetch()

    assert seller.global_id == "pandape:7"
    assert str(seller.url) == "https://acme.pandape.infojobs.com.br/Detail/7"
    assert (seller.title, seller.company) == ("Vendedor", "Acme Brasil")
    assert (seller.location, seller.country_iso) == ("Uberaba, MG", "BR")
    assert (seller.workplace_type, seller.is_remote) == ("onsite", None)
    assert (seller.employment_type, seller.engagement_type) == ("FULL_TIME", "permanent")
    assert seller.commitment == "Período Integral"
    assert (seller.salary_min, seller.salary_max, seller.salary_currency) == (
        2000.0, 2200.0, "BRL",
    )
    assert seller.salary_summary == "R$ 2.000,00 - R$ 2.200,00"
    assert seller.raw == {"contract": "Efetivo – CLT", "openings": 3}
    assert seller.description is None
    assert (intern.workplace_type, intern.employment_type, intern.engagement_type) == (
        "hybrid", "INTERN", "internship",
    )
    assert (intern.location, intern.salary_min) == ("São Paulo, SP", None)


@pytest.mark.parametrize(
    ("contract", "schedule", "employment", "engagement"),
    [
        ("Temporário", "Período Integral", "TEMPORARY", "temporary"),
        ("Prestador de Serviços (PJ)", "Período Integral", "CONTRACT", "independent_contractor"),
        ("Autônomo", "Parcial tardes", "CONTRACT", "independent_contractor"),
        ("Aprendiz", "Parcial manhãs", "PART_TIME", "apprenticeship"),
        ("Outros", "Por turnos", None, None),
    ],
)
def test_reads_the_contract_and_the_hours(
    httpx_mock, contract: str, schedule: str, employment: str | None, engagement: str | None,
) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([
        _card(7, "Vaga", _fact("clock", schedule), _fact("sheet", contract)),
    ]))

    job = PandapeScraper("acme", include_descriptions=False).fetch()[0]

    assert (job.employment_type, job.engagement_type) == (employment, engagement)


def test_a_remote_vacancy_and_a_place_without_a_state(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([
        _card(7, "Dev", _fact("location-pin-1", "Lisboa"), _fact("house", "Remoto")),
    ]))

    job = PandapeScraper("acme", include_descriptions=False).fetch()[0]

    assert (job.location, job.country_iso) == ("Lisboa", None)
    assert (job.workplace_type, job.is_remote) == ("remote", True)


def test_takes_the_company_name_from_the_title_when_the_page_has_no_heading(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(7, "Vaga")], heading=None))

    assert PandapeScraper("acme", include_descriptions=False).fetch()[0].company == "Acme Brasil"


def test_turns_every_page_and_keeps_a_vacancy_once(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(pandape, "PAGE_SIZE", 2)
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A"), _card(2, "B")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2, 2),
        json=_more([_card(2, "B"), _card(3, "C")], last=False, total=5),
    )
    httpx_mock.add_response(
        method="POST", url=_page(3, 2),
        json=_more([_card(4, "D"), _card(5, "E")], last=True, total=5),
    )

    jobs = PandapeScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3", "4", "5"]


def test_keeps_turning_while_the_list_grows(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(pandape, "PAGE_SIZE", 2)
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A"), _card(2, "B")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2, 2),
        json=_more([_card(3, "C"), _card(4, "D")], last=False, total=4),
    )
    httpx_mock.add_response(
        method="POST", url=_page(3, 2), json=_more([_card(5, "E")], last=True, total=5),
    )

    jobs = PandapeScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3", "4", "5"]


def test_an_answer_without_a_list_ends_the_reading(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(method="POST", url=_page(2), json={"counter": "", "isLast": True})

    assert len(PandapeScraper("acme", include_descriptions=False).fetch()) == 1


def test_a_list_longer_than_the_page_limit_is_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(pandape, "MAX_PAGES", 2)
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2), json=_more([_card(2, "B")], last=False, total=60),
    )

    with pytest.raises(ScraperError, match="more than 2 pages"):
        PandapeScraper("acme", include_descriptions=False).fetch()


def test_a_list_still_turning_at_the_page_limit_is_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(pandape, "MAX_PAGES", 2)
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2), json=_more([_card(2, "B")], last=False, total=40),
    )

    with pytest.raises(ScraperError, match="more than 2 pages"):
        PandapeScraper("acme", include_descriptions=False).fetch()


def test_an_unreadable_page_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(method="POST", url=_page(2), text="<html>busy</html>")

    with pytest.raises(ScraperError, match="unreadable page 2"):
        PandapeScraper("acme", include_descriptions=False).fetch()


def test_the_year_of_a_listed_day_follows_the_order_of_the_list() -> None:
    def job(job_id: str):
        return PandapeScraper("acme")._jobs(pandape.parse_html(_card(int(job_id), "Vaga")))[0]

    jobs = [job(str(number)) for number in range(1, 8)]
    listed = {
        "1": (11, 19),  # this year
        "2": (1, 3),  # this year
        "3": (12, 30),  # the year before
        "4": (1, 2),  # a neighbour a few days out of order: still this year
        "5": (8, 20),  # the year before
        "6": (10, 15),  # two years before
        "7": (2, 30),  # not a day
    }

    pandape._date_in_order(jobs, listed, date(2026, 11, 20))

    assert [found.posted_at.date().isoformat() if found.posted_at else None for found in jobs] == [
        "2026-11-19", "2026-01-03", "2025-12-30", "2026-01-02", "2025-08-20", "2024-10-15", None,
    ]
    assert jobs[0].posted_at == datetime(2026, 11, 19, 3, tzinfo=UTC)


def test_a_day_ahead_of_today_belongs_to_last_year() -> None:
    card = PandapeScraper("acme")._jobs(pandape.parse_html(_card(1, "Vaga")))[0]

    pandape._date_in_order([card], {"1": (12, 24)}, date(2026, 11, 20))

    assert card.posted_at is not None
    assert card.posted_at.date() == date(2025, 12, 24)


def test_reads_the_description_from_the_vacancy_page(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([
        _card(7, "Vendedor", _fact("wallet", "R$ 2.000,00"), _fact("clock", "Por turnos")),
        _card(8, "Caixa"),
    ]))
    httpx_mock.add_response(
        url="https://acme.pandape.infojobs.com.br/Detail/7", text=_posting(),
    )
    httpx_mock.add_response(
        url="https://acme.pandape.infojobs.com.br/Detail/8",
        text=_posting(
            hiringOrganization={"@type": "Organization", "name": "Empresa com nome confidencial"},
            baseSalary=None,
            employmentType="part-time",
        ),
    )

    seller, cashier = PandapeScraper("acme").fetch()

    assert seller.description == "Atender clientes.\xa0\nOrganizar a loja."
    assert seller.company == "Acme Varejo"
    assert (seller.location, seller.country_iso) == ("Uberaba, MG", "BR")
    assert seller.posted_at == datetime(2026, 11, 12, 15, 36, 10, 998242, tzinfo=UTC)
    assert seller.employment_type == "FULL_TIME"
    assert (seller.salary_min, seller.salary_max) == (2000.0, 2200.0)
    assert (seller.salary_currency, seller.salary_period) == ("BRL", "MONTH")
    assert seller.salary_summary == "2000 - 2200 per month"
    assert cashier.company == "Acme Brasil"
    assert cashier.employment_type == "PART_TIME"
    assert cashier.salary_min is None


def test_without_descriptions_no_vacancy_page_is_read(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(7, "Vendedor")]))

    job = PandapeScraper("acme", include_descriptions=False).fetch()[0]

    assert job.description is None
    assert len(httpx_mock.get_requests()) == 1


def test_keeps_a_vacancy_whose_page_is_gone(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(7, "Vendedor")]))
    httpx_mock.add_response(url="https://acme.pandape.infojobs.com.br/Detail/7", status_code=404)

    job = PandapeScraper("acme").fetch()[0]

    assert (job.title, job.description) == ("Vendedor", None)


def _printed(job_id: int, text: str) -> str:
    return (
        "<html><head>"
        f'<meta name="description" content="{text}" />'
        "</head><body>"
        f'<a href="/Apply/{job_id}">Candidate-se</a>'
        "</body></html>"
    )


def test_reads_the_text_of_a_vacancy_page_without_posting_data(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(7, "Vendedor")]))
    httpx_mock.add_response(
        url="https://acme.pandape.infojobs.com.br/Detail/7",
        text=_printed(7, "Atender clientes.&#xA;Organizar a loja &amp; o estoque."),
    )

    job = PandapeScraper("acme").fetch()[0]

    assert job.description == "Atender clientes.\nOrganizar a loja & o estoque."
    assert job.company == "Acme Brasil"


def test_a_page_that_is_not_of_the_vacancy_gives_no_text(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(7, "Vendedor"), _card(8, "Caixa")]))
    httpx_mock.add_response(
        url="https://acme.pandape.infojobs.com.br/Detail/7", text=_printed(70, "Outra vaga."),
    )
    httpx_mock.add_response(
        url="https://acme.pandape.infojobs.com.br/Detail/8",
        text='<html><head><meta name="description" content="Vagas abertas." /></head></html>',
    )

    seller, cashier = PandapeScraper("acme").fetch()

    assert (seller.description, cashier.description) == (None, None)


def test_a_vacancy_page_with_an_empty_description_tag_gives_no_text(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(7, "Vendedor")]))
    httpx_mock.add_response(
        url="https://acme.pandape.infojobs.com.br/Detail/7", text=_printed(7, " "),
    )

    assert PandapeScraper("acme").fetch()[0].description is None


def test_a_company_with_nothing_open_is_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([]))

    assert PandapeScraper("acme").fetch() == []


def test_a_closed_career_page_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, status_code=404, text="<html>fechada</html>")

    with pytest.raises(CompanyNotFoundError):
        PandapeScraper("acme").fetch()


def test_a_page_without_a_vacancy_list_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text="<html><body>Em manutenção</body></html>")

    with pytest.raises(ScraperError, match="unrecognized career page"):
        PandapeScraper("acme").fetch()


@pytest.mark.parametrize("slug", ["", "acme.pandape.infojobs.com.br", "https://acme", "empregos", "www"])
def test_rejects_what_is_not_a_company(slug: str) -> None:
    with pytest.raises(ScraperError):
        PandapeScraper(slug)


def test_a_list_that_breaks_off_twice_is_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(pandape, "PAGE_SIZE", 1)
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False), is_reusable=True)
    httpx_mock.add_response(
        method="POST", url=_page(2, 1), is_reusable=True,
        json=_more([_card(2, "B")], last=False, total=4),
    )
    httpx_mock.add_response(
        method="POST", url=_page(3, 1), json={"isLast": True}, is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST", url=_page(4, 1), is_reusable=True,
        json=_more([_card(4, "D")], last=True, total=4),
    )

    with pytest.raises(ScraperError, match="broke off at page 3 of 4"):
        PandapeScraper("acme", include_descriptions=False).fetch()

    assert len(httpx_mock.get_requests()) == 8


def test_a_list_that_shrank_while_it_was_read_is_read_again(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(pandape, "PAGE_SIZE", 1)
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2, 1), json=_more([_card(2, "B")], last=False, total=3),
    )
    httpx_mock.add_response(
        method="POST", url=_page(3, 1), json={"view": "<p>Nenhuma vaga</p>", "isLast": True},
    )
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2, 1), json=_more([_card(2, "B")], last=True, total=2),
    )

    jobs = PandapeScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]


def test_a_vacancy_that_moved_between_two_pages_is_found_on_the_second_reading(
    httpx_mock, monkeypatch,
) -> None:
    monkeypatch.setattr(pandape, "PAGE_SIZE", 2)
    first = _list([_card(1, "A"), _card(2, "B")], last=False)
    httpx_mock.add_response(url=LIST_URL, text=first)
    # A vacancy was published between the two reads: 2 is listed again and 3 is skipped.
    httpx_mock.add_response(
        method="POST", url=_page(2, 2),
        json=_more([_card(2, "B"), _card(4, "D")], last=True, total=4),
    )
    httpx_mock.add_response(url=LIST_URL, text=first)
    httpx_mock.add_response(
        method="POST", url=_page(2, 2),
        json=_more([_card(3, "C"), _card(4, "D")], last=True, total=4),
    )

    jobs = PandapeScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3", "4"]


def test_a_list_that_stays_short_is_read_twice_and_then_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(pandape, "PAGE_SIZE", 2)
    httpx_mock.add_response(
        url=LIST_URL, text=_list([_card(1, "A"), _card(2, "B")], last=False), is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST", url=_page(2, 2), is_reusable=True,
        json=_more([_card(2, "B")], last=True, total=4),
    )

    with pytest.raises(ScraperError, match="listed 2 of the 4 vacancies it announces"):
        PandapeScraper("acme", include_descriptions=False).fetch()

    assert len(httpx_mock.get_requests()) == 4


@pytest.mark.parametrize(
    ("status", "words"),
    [(429, "returned 429 after"), (503, "returned 503 after"), (403, "returned 403")],
)
def test_a_refused_company_page_is_an_error_not_an_empty_company(
    httpx_mock, status: int, words: str,
) -> None:
    httpx_mock.add_response(url=LIST_URL, status_code=status, is_reusable=True)

    with pytest.raises(ScraperError, match=words) as caught:
        PandapeScraper("acme").fetch()

    assert not isinstance(caught.value, CompanyNotFoundError)


@pytest.mark.parametrize(
    ("status", "words"),
    [(429, "returned 429 after"), (500, "returned 500 after"), (403, "returned 403")],
)
def test_a_page_refused_midway_fails_the_whole_list(httpx_mock, status: int, words: str) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2), json=_more([_card(2, "B")], last=False, total=80),
    )
    httpx_mock.add_response(method="POST", url=_page(3), status_code=status, is_reusable=True)
    httpx_mock.add_response(
        method="POST", url=_page(4), json=_more([_card(4, "D")], last=True, total=80),
        is_optional=True,
    )

    with pytest.raises(ScraperError, match=words):
        PandapeScraper("acme", include_descriptions=False).fetch()


def test_a_list_page_that_goes_missing_is_an_error_not_a_closed_company(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(method="POST", url=_page(2), status_code=404)

    with pytest.raises(ScraperError, match="page 2 of the list returned 404") as caught:
        PandapeScraper("acme", include_descriptions=False).fetch()

    assert not isinstance(caught.value, CompanyNotFoundError)


@pytest.mark.parametrize(
    ("status", "words"),
    [(429, "returned 429 after"), (502, "returned 502 after"), (403, "returned 403")],
)
def test_a_refused_vacancy_page_fails_the_read(httpx_mock, status: int, words: str) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(7, "Vendedor")]))
    httpx_mock.add_response(
        url="https://acme.pandape.infojobs.com.br/Detail/7", status_code=status, is_reusable=True,
    )

    with pytest.raises(ScraperError, match=words):
        PandapeScraper("acme").fetch()


@pytest.mark.parametrize("answer", [{"isLast": False, "subtotal": 80}, {"view": "", "isLast": False}])
def test_a_page_that_lists_nothing_but_announces_more_is_an_error(
    httpx_mock, answer: dict[str, object],
) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(method="POST", url=_page(2), json=answer)

    with pytest.raises(ScraperError, match="page 2 lists nothing although more is announced"):
        PandapeScraper("acme", include_descriptions=False).fetch()


def test_a_list_that_stops_growing_on_an_empty_page_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(
        method="POST", url=_page(2), json=_more([_card(2, "B")], last=False, total=40),
    )
    httpx_mock.add_response(method="POST", url=_page(3), json=_more([], last=False, total=41))

    with pytest.raises(ScraperError, match="page 3 lists nothing although more is announced"):
        PandapeScraper("acme", include_descriptions=False).fetch()


@pytest.mark.parametrize("answer", [[], "busy", None])
def test_an_answer_that_is_not_a_page_is_an_error(httpx_mock, answer: object) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A")], last=False))
    httpx_mock.add_response(method="POST", url=_page(2), json=answer)

    with pytest.raises(ScraperError, match="unreadable page 2"):
        PandapeScraper("acme", include_descriptions=False).fetch()


def test_a_second_reading_takes_the_days_printed_then(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A", day="3 mar")]))
    httpx_mock.add_response(url=LIST_URL, text=_list([_card(1, "A", day="9 jun")]))
    scraper = PandapeScraper("acme", include_descriptions=False)

    first = scraper.fetch()[0].posted_at
    second = scraper.fetch()[0].posted_at

    assert first is not None and second is not None
    assert (first.month, first.day) == (3, 3)
    assert (second.month, second.day) == (6, 9)


def test_a_first_page_that_lists_nothing_but_announces_more_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_list([], last=False))

    with pytest.raises(ScraperError, match="page 1 lists nothing although more is announced"):
        PandapeScraper("acme", include_descriptions=False).fetch()
