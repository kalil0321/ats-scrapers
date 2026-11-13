"""Tests for the Cegid Talentsoft career-site scraper."""

from __future__ import annotations

import html
import re

import httpx
import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, TalentsoftScraper
from ats_scrapers.scrapers import talentsoft as module
from ats_scrapers.scrapers.talentsoft import _parse_listing, _parse_offer, _parse_slug

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py.

BASE = "https://acme-recrute.talent-soft.com"
LIST = f"{BASE}/offre-de-emploi/liste-toutes-offres.aspx"
LIST_RE = re.compile(rf"^{re.escape(LIST)}\?mode=list&page=\d+$")
PAGE_SIZE = 10
PAGE_CAP = 100


def _path(number: int, folder: str = "offre-de-emploi") -> str:
    return f"/{folder}/emploi-acheteur-f-h_{number}.aspx"


def _item(
    number: int,
    title: str = "Acheteur F/H",
    *,
    reference: str | None = "2026-1",
    listed: tuple[str, ...] = ("Réf. : 2026-1", "13/11/2026", "CDI", "Anglet"),
    folder: str = "offre-de-emploi",
) -> str:
    name = html.escape(title)
    favorite = (
        f'<span class="ts-offer-list-item-favorite-link" data-reference="{reference}" '
        f'data-title="{name}"><img alt="Ajouter {name}" src="/x.gif" /></span>'
        if reference is not None
        else ""
    )
    lines = "".join(f"<li>{html.escape(line)}</li>" for line in listed)
    return (
        '<li class="ts-offer-list-item offerlist-item " '
        f"onclick=\"location.href='{_path(number, folder)}';\">"
        '<h3 class="ts-offer-list-item__title styleh3">'
        f'<a class="ts-offer-list-item__title-link " href="{_path(number, folder)}" '
        f'title="{name} (Réf. : {reference})">\n {name}\n</a></h3>{favorite}'
        f'<ul class="ts-offer-list-item__description ">{lines}</ul></li>'
    )


def _pager(page: int, last: int) -> str:
    def entry(number: int) -> str:
        if number == page:
            return (
                '<li class="ts-ol-pagination-list-item ts-ol-pagination-list-item--active">'
                f'<span class="ts-ol-pagination-list-item__link--active">{number}</span>&nbsp;</li>'
            )
        return (
            '<li class="ts-ol-pagination-list-item ts-ol-pagination-list-item--other">'
            f'<a title="page {number} / {last}" class="ts-ol-pagination-list-item__link" '
            f'href="{LIST}?page={number}&amp;LCID=1036">{number}</a>&nbsp;</li>'
        )

    # Like the site: the pages around the current one, and the last one.
    shown = sorted({n for n in (page - 1, page, page + 1, last) if 1 <= n <= last})
    return (
        '<div id="resultatPagination" class="pagination">'
        f'<ul class="ts-ol-pagination-list">{"".join(entry(n) for n in shown)}</ul></div>'
    )


def _list_page(
    items: list[str],
    *,
    total: int | None,
    page: int = 1,
    last: int = 1,
    lang: str = "fr",
) -> str:
    """One page of the list: count, pager and offers; a "no result" block when
    there is no offer."""
    if total:
        body = (
            '<div id="ctl00_ctl00_corpsRoot_corps_resultat">Nombre de résultats : '
            f'<span id="ctl00_ctl00_corpsRoot_corps_Pagination_TotalOffers">{total} offre(s)'
            f"</span>{_pager(page, last)}</div>"
            f'<div id="main"><ul class="ts-related-offers__row">{"".join(items)}</ul></div>'
        )
    else:
        body = '<div id="ctl00_ctl00_corpsRoot_corps_NoResult_NoResult">Aucune offre</div>'
    return (
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">'
        f"<head><title> ACME - Résultat de votre recherche ({total or 0} offres, "
        f"page {page}) / Tout afficher </title></head><body>"
        '<p id="ctl00_ctl00_corpsRoot_corps_AfficheMoteur_afficherUrlCritere"></p>'
        f"{body}</body></html>"
    )


def _serve(httpx_mock, items: list[str], *, total: int | None = None, **options: object) -> None:
    """Answer the list the way a site does: ten offers a page, a hundred
    pages at most, and the first page again past the end."""
    count = len(items) if total is None else total
    last = min(max(1, -(-len(items) // PAGE_SIZE)), PAGE_CAP)

    def reply(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page > last:
            page = 1
        chunk = items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return httpx.Response(
            200, text=_list_page(chunk, total=count, page=page, last=last, **options)
        )

    httpx_mock.add_callback(reply, url=LIST_RE, is_reusable=True)


def _pages(httpx_mock) -> list[int]:
    return [
        int(request.url.params["page"])
        for request in httpx_mock.get_requests()
        if request.url.path.endswith("liste-toutes-offres.aspx")
    ]


def _offer_page(content: str) -> str:
    return (
        "<html><head><title>ACME - Acheteur F/H</title></head><body>"
        '<div id="detail_offre"><h1>Détail de l\'offre</h1>'
        f'<div class="ts-offer-page__content-details" id="contenu-ficheoffre">{content}</div>'
        '</div><div id="footer">Mentions légales</div></body></html>'
    )


def _serve_offer(httpx_mock, number: int, content: str, folder: str = "offre-de-emploi") -> None:
    httpx_mock.add_response(
        url=f"{BASE}{_path(number, folder)}", text=_offer_page(content), is_reusable=True
    )


def _scraper(**options: object) -> TalentsoftScraper:
    options.setdefault("include_descriptions", False)
    return TalentsoftScraper("acme-recrute", **options)


# --- Registry and slug ------------------------------------------------------


def test_registry_resolves_talentsoft() -> None:
    assert ScraperRegistry.get(ATSType.TALENTSOFT) is TalentsoftScraper


@pytest.mark.parametrize(
    "slug",
    [
        "acme-recrute",
        " ACME-Recrute ",
        "acme-recrute.talent-soft.com",
        "https://acme-recrute.talent-soft.com",
        "https://ACME-recrute.talent-soft.com/offre-de-emploi/liste-toutes-offres.aspx?page=2",
        "https://acme-recrute.talent-soft.com/job/job-buyer_15653.aspx",
    ],
)
def test_slug_forms(slug: str) -> None:
    assert _parse_slug(slug) == "acme-recrute"
    assert TalentsoftScraper(slug).base_url == BASE


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "   ",
        "-acme",
        "acme-",
        "acme/offre-de-emploi",
        "acme recrute",
        "ac\nme",
        "acme.example.com",
        "a.b.talent-soft.com",
        "http://acme-recrute.talent-soft.com",
        "https://talent-soft.com/",
        "https://a.b.talent-soft.com/",
        "https://acme-recrute.talent-soft.com.evil.com/",
        "https://evil.com/acme-recrute.talent-soft.com",
        "https://user:pw@acme-recrute.talent-soft.com/",
        "https://acme-recrute.talent-soft.com:8443/",
    ],
)
def test_invalid_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(ScraperError):
        TalentsoftScraper(slug)


# --- Offer list -------------------------------------------------------------


def test_parses_a_listing(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [
            _item(15653, 'Stage "Achats & logistique" F/H', reference="2026-15653"),
            _item(15642, "Gestionnaire de production F/H", reference="2026-15642", listed=()),
        ],
    )

    jobs = _scraper(company_name=" Acme Aviation ").fetch()

    assert [job.title for job in jobs] == [
        'Stage "Achats & logistique" F/H',
        "Gestionnaire de production F/H",
    ]
    first = jobs[0]
    assert str(first.url) == f"{BASE}/offre-de-emploi/emploi-acheteur-f-h_15653.aspx"
    assert first.company == "Acme Aviation"
    assert first.ats_type is ATSType.TALENTSOFT
    assert first.ats_id == "acme-recrute:15653"
    assert first.global_id == "talentsoft:acme-recrute:15653"
    assert first.requisition_id == "2026-15653"
    assert first.language == "fr"
    assert first.description is None
    # The lines under the title carry no label: kept as listed, not read.
    assert first.raw == {"listed": ["Réf. : 2026-1", "13/11/2026", "CDI", "Anglet"]}
    assert first.posted_at is None
    assert first.location is None
    assert first.commitment is None
    assert jobs[1].raw is None
    assert _pages(httpx_mock) == [1]


def test_company_falls_back_to_the_site_label(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1)])

    assert _scraper().fetch()[0].company == "acme-recrute"
    assert _scraper(company_name="  ").fetch()[0].company == "acme-recrute"


@pytest.mark.parametrize(
    ("folder", "lang"), [("job", "en"), ("stelle", "de"), ("vacature", "nl-NL")]
)
def test_offer_pages_of_sites_in_other_languages(httpx_mock, folder: str, lang: str) -> None:
    _serve(httpx_mock, [_item(2069, "Buyer", folder=folder)], lang=lang)

    job = _scraper().fetch()[0]

    assert str(job.url) == f"{BASE}/{folder}/emploi-acheteur-f-h_2069.aspx"
    assert job.language == lang[:2]


def test_a_page_without_a_language_gives_none(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1)], lang="")

    assert _scraper().fetch()[0].language is None


def test_an_offer_without_a_reference(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1, reference=None)])

    assert _scraper().fetch()[0].requisition_id is None


def test_a_link_to_something_else_than_an_offer_is_not_an_offer() -> None:
    """A row whose title links elsewhere (another host, a job-profile sheet
    without a number) cannot be read."""
    other_host = _item(1).replace(
        'href="/offre-de-emploi/', 'href="https://elsewhere.example/offre-de-emploi/'
    )
    no_number = _item(2).replace("_2.aspx", ".aspx")
    listing = _parse_listing(_list_page([other_host, no_number, _item(3)], total=3))

    assert [row.number for row in listing.rows] == ["3"]
    assert len(listing.identities) == 3
    assert listing.identities[2] == "3"
    assert all(key.startswith("?") for key in listing.identities[:2])


# --- Walk -------------------------------------------------------------------


def test_walks_every_page(httpx_mock) -> None:
    _serve(httpx_mock, [_item(n, f"Poste {n}") for n in range(1, 26)])

    jobs = _scraper().fetch()

    assert [job.ats_id for job in jobs] == [f"acme-recrute:{n}" for n in range(1, 26)]
    # The pager of the last page names no page after it: no further request.
    assert _pages(httpx_mock) == [1, 2, 3]


def test_a_site_that_lists_everything_on_one_page(httpx_mock) -> None:
    page = _list_page([_item(n) for n in range(1, 155)], total=154)
    httpx_mock.add_response(url=LIST_RE, text=page, is_reusable=True)

    assert len(_scraper().fetch()) == 154
    assert _pages(httpx_mock) == [1]


def test_an_empty_site_returns_no_job(httpx_mock) -> None:
    _serve(httpx_mock, [])

    assert _scraper().fetch() == []
    assert _pages(httpx_mock) == [1]


def test_a_page_past_the_end_answers_the_first_page_and_ends_the_walk(httpx_mock) -> None:
    """An offer closes during the walk: the third page announced on page 1 no
    longer exists, and asking for it answers page 1."""
    items = [_item(n) for n in range(1, 21)]
    first = _list_page(items[:10], total=21, page=1, last=3)
    second = _list_page(items[10:], total=20, page=2, last=2)
    again = _list_page(items[:10], total=20, page=1, last=2)
    httpx_mock.add_response(url=f"{LIST}?mode=list&page=1", text=first)
    httpx_mock.add_response(url=f"{LIST}?mode=list&page=2", text=second)
    httpx_mock.add_response(url=f"{LIST}?mode=list&page=3", text=again)

    assert len(_scraper().fetch()) == 20
    assert _pages(httpx_mock) == [1, 2, 3]


def test_a_list_that_gives_far_fewer_offers_than_announced_is_an_error(httpx_mock) -> None:
    """Four pages announced, and every page after the first answers page 1."""
    first = _list_page([_item(n) for n in range(1, 11)], total=40, page=1, last=4)
    httpx_mock.add_response(url=LIST_RE, text=first, is_reusable=True)

    with pytest.raises(ScraperError, match="only 10 of the 40 offers"):
        _scraper().fetch()
    assert _pages(httpx_mock) == [1, 2]


def test_an_offer_listed_on_two_pages_is_kept_once(httpx_mock) -> None:
    first = [_item(n) for n in range(1, 11)]
    second = [_item(10), _item(11)]
    httpx_mock.add_response(
        url=f"{LIST}?mode=list&page=1", text=_list_page(first, total=11, page=1, last=2)
    )
    httpx_mock.add_response(
        url=f"{LIST}?mode=list&page=2", text=_list_page(second, total=11, page=2, last=2)
    )

    jobs = _scraper().fetch()

    assert [job.ats_id for job in jobs] == [f"acme-recrute:{n}" for n in range(1, 12)]


def test_a_list_capped_at_a_hundred_pages_is_refused_on_its_first_page(httpx_mock) -> None:
    _serve(httpx_mock, [_item(n) for n in range(1, 1046)])

    with pytest.raises(ScraperError, match=r"announces 1045 offers .* page 100 \(1000 offers\)"):
        _scraper().fetch()
    assert _pages(httpx_mock) == [1]


def test_a_cut_list_is_refused_even_one_offer_past_its_last_page(httpx_mock) -> None:
    _serve(httpx_mock, [_item(n) for n in range(1, 1002)])

    with pytest.raises(ScraperError, match=r"announces 1001 offers .* \(1000 offers\)"):
        _scraper().fetch()
    assert _pages(httpx_mock) == [1]


def test_a_list_of_exactly_a_hundred_pages_is_read(httpx_mock) -> None:
    _serve(httpx_mock, [_item(n) for n in range(1, 1001)])

    assert len(_scraper().fetch()) == 1000
    assert _pages(httpx_mock) == list(range(1, 101))


def test_an_unreadable_offer_counts_against_the_floor(httpx_mock) -> None:
    broken = '<li class="ts-offer-list-item offerlist-item "><h3>Sans lien</h3></li>'
    _serve(httpx_mock, [_item(n) for n in range(1, 10)] + [broken])

    assert len(_scraper().fetch()) == 9

    httpx_mock.reset()
    _serve(
        httpx_mock,
        [_item(n) for n in range(1, 9)] + [broken, broken.replace("Sans lien", "Autre")],
    )

    with pytest.raises(ScraperError, match="only 8 of the 10 offers"):
        _scraper().fetch()


def test_a_page_of_unreadable_offers_does_not_pass_for_the_end(httpx_mock) -> None:
    def broken(n: int) -> str:
        return f'<li class="ts-offer-list-item offerlist-item "><h3>Poste {n}</h3></li>'

    items = (
        [_item(n) for n in range(1, 11)]
        + [broken(n) for n in range(11, 21)]
        + [_item(n) for n in range(21, 31)]
    )
    _serve(httpx_mock, items)

    with pytest.raises(ScraperError, match="only 20 of the 30 offers"):
        _scraper().fetch()
    assert _pages(httpx_mock) == [1, 2, 3]


def test_the_page_guard_refuses_a_partial_list(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_PAGES", 2)
    _serve(httpx_mock, [_item(n) for n in range(1, 31)])

    with pytest.raises(ScraperError, match="page guard 2"):
        _scraper().fetch()
    assert _pages(httpx_mock) == [1, 2]


def test_a_page_that_is_not_an_offer_list_is_an_error(httpx_mock) -> None:
    """A site that sends visitors to its log-in page (internal mobility)."""
    httpx_mock.add_response(
        url=f"{LIST}?mode=list&page=1",
        text="<html><head><title>ACME - Mon espace candidat</title></head>"
        '<body><div id="ctl00_ctl00_corpsRoot_corps_pnlAccount">Connexion</div></body></html>',
    )

    with pytest.raises(ScraperError, match=r"not an offer list.*Mon espace candidat"):
        _scraper().fetch()


def test_a_missing_site_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{LIST}?mode=list&page=1", status_code=404)

    with pytest.raises(CompanyNotFoundError):
        _scraper().fetch()


def test_a_failing_page_fails_the_scrape(httpx_mock) -> None:
    first = _list_page([_item(n) for n in range(1, 11)], total=15, page=1, last=2)
    httpx_mock.add_response(url=f"{LIST}?mode=list&page=1", text=first)
    httpx_mock.add_response(url=f"{LIST}?mode=list&page=2", status_code=500, is_reusable=True)

    with pytest.raises(ScraperError):
        _scraper().fetch()


def test_the_listing_reads_count_pager_and_language() -> None:
    listing = _parse_listing(
        _list_page([_item(n) for n in range(11, 21)], total=1204, page=2, last=100)
    )

    assert listing.is_offer_list
    assert listing.total == 1204
    assert listing.page == 2
    assert listing.has_next
    assert listing.last_page == 100
    assert listing.language == "fr"
    assert listing.title == "ACME - Résultat de votre recherche (1204 offres, page 2) / Tout afficher"


# --- Offer page -------------------------------------------------------------


STANDARD_OFFER = (
    "<h2>Informations générales</h2>"
    '<div class="ts-offer-page__reference"><h3>Référence</h3> 2026-18516&nbsp;&nbsp;</div>'
    '<h2 class="JobDescription">Description du poste</h2>'
    '<h3>Type de contrat</h3><p id="fldjobdescription_contract">CDD-Temps plein</p>'
    '<h3>Intitulé du poste</h3><p id="fldjobdescription_jobtitle">Acheteur F/H</p>'
    '<h3>Description de la mission</h3><div id="fldjobdescription_description1">'
    "<p>Vous achetez &amp; négociez.</p><p>&nbsp;</p><ul><li>Appels d'offres</li>"
    "<li>Suivi</li></ul></div>"
    '<h3>Rémunération</h3><p id="fldjobdescription_salaryrange">35 à 40 k€ brut annuel</p>'
    "<h2>Localisation du poste</h2>"
    '<h3>Localisation du poste</h3><p id="fldlocation_location_geographicalareacollection">'
    "Europe, France, Ile-de-France, Val d'Oise (95)</p>"
    '<h3>Lieu</h3><p id="fldlocation_joblocation">Bonneuil-en-France</p>'
)


def test_the_offer_page_gives_the_description_and_the_fields_read_by_id(httpx_mock) -> None:
    _serve(httpx_mock, [_item(18516, reference="2026-18516")])
    _serve_offer(httpx_mock, 18516, STANDARD_OFFER)

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.commitment == "CDD-Temps plein"
    assert job.salary_summary == "35 à 40 k€ brut annuel"
    # The place, then the area it is in.
    assert job.location == "Bonneuil-en-France, Europe, France, Ile-de-France, Val d'Oise (95)"
    assert job.description == (
        "Informations générales\nRéférence\n2026-18516\nDescription du poste\n"
        "Type de contrat\nCDD-Temps plein\nIntitulé du poste\nAcheteur F/H\n"
        "Description de la mission\nVous achetez & négociez.\nAppels d'offres\nSuivi\n"
        "Rémunération\n35 à 40 k€ brut annuel\nLocalisation du poste\n"
        "Localisation du poste\nEurope, France, Ile-de-France, Val d'Oise (95)\n"
        "Lieu\nBonneuil-en-France"
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('<p id="fldlocation_joblocation">Dijon</p>', "Dijon"),
        (
            '<p id="fldlocation_location_geographicalareacollection">Aargau, Hendschiken</p>',
            "Aargau, Hendschiken",
        ),
        ('<p id="FldLocation_JobLocation"> Turnhout </p>', "Turnhout"),
        ('<p id="fldlocation_joblocation"> </p><p>Texte</p>', None),
    ],
)
def test_location_from_the_place_and_area_fields(content: str, expected: str | None) -> None:
    assert _parse_offer(_offer_page(content)).location == expected


def test_an_offer_page_laid_out_by_the_customer_still_gives_its_text(httpx_mock) -> None:
    """No product field id: the text is the description, nothing else is read."""
    _serve(httpx_mock, [_item(16008)])
    _serve_offer(
        httpx_mock,
        16008,
        "<span><table><tr><td><p>Dassault conçoit des avions.<br>Depuis un siècle.</p></td>"
        "<td><div>Localisation</div><div>France, Nouvelle Aquitaine, 64 - Biarritz</div></td>"
        "</tr></table></span><script>track()</script>",
    )

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description == (
        "Dassault conçoit des avions.\nDepuis un siècle.\nLocalisation\n"
        "France, Nouvelle Aquitaine, 64 - Biarritz"
    )
    assert job.location is None
    assert job.commitment is None
    assert job.salary_summary is None


def test_an_offer_page_that_fails_keeps_the_offer(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1), _item(2, "Comptable")])
    httpx_mock.add_response(url=f"{BASE}{_path(1)}", status_code=500, is_reusable=True)
    _serve_offer(httpx_mock, 2, "<p>Tient les comptes.</p>")

    jobs = _scraper(include_descriptions=True).fetch()

    assert [job.description for job in jobs] == [None, "Tient les comptes."]


def test_a_page_without_the_offer_block_gives_no_description(httpx_mock) -> None:
    """An offer closed since the list was read answers another page."""
    _serve(httpx_mock, [_item(1)])
    httpx_mock.add_response(
        url=f"{BASE}{_path(1)}",
        text="<html><body><p id=\"fldjobdescription_contract\"></p>Offre pourvue</body></html>",
    )

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description is None
    assert job.commitment is None


def test_the_description_is_capped(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(module, "MAX_DESCRIPTION_CHARS", 30)
    _serve(httpx_mock, [_item(1)])
    _serve_offer(httpx_mock, 1, "<p>" + "mot " * 50 + "</p>")

    job = _scraper(include_descriptions=True).fetch()[0]

    assert job.description is not None
    assert len(job.description) <= 30
    assert job.description.startswith("mot mot")


def test_get_description_fetches_one_offer(httpx_mock) -> None:
    _serve(httpx_mock, [_item(1)])
    _serve_offer(httpx_mock, 1, "<p>Achète.</p>")
    scraper = _scraper()
    job = scraper.fetch()[0]
    assert job.description is None

    assert scraper.get_description(job) == "Achète."
    assert scraper.get_description(job) == "Achète."
    assert len([r for r in httpx_mock.get_requests() if r.url.path == _path(1)]) == 1
