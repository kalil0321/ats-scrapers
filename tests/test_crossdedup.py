"""Title passes of the cross-source dedup (``pipeline/crossdedup.py``).

Every case is a small keys frame run through the publisher's own
``_decide_dedup_survivors_polars``, so the rules are tested where they
are used.
"""

from __future__ import annotations

import polars as pl
import pytest

from pipeline.crossdedup import company_key, place_slots, title_base, title_key
from pipeline.publisher import _decide_dedup_survivors_polars

# --- helpers -----------------------------------------------------------------


def _survivors(rows: list[tuple[str, int, str, str, str]], **fields: str) -> dict[str, int]:
    """Kept rows per source for ``(source, priority, title, company, location)`` rows."""
    keys = pl.DataFrame(
        [
            {
                "_local_idx": index,
                "_orig_idx": index,
                "_priority": priority,
                "ats_type": source,
                "url": f"https://{source}.example/{index}",
                "title_raw": title,
                "title": title.lower(),
                "company": company.lower(),
                "location": location.lower(),
                "ats_id": f"{source}-{index}",
                **fields,
            }
            for index, (source, priority, title, company, location) in enumerate(rows)
        ]
    )
    return {source: frame.height for source, frame in _decide_dedup_survivors_polars(keys).items()}


def _shops(source: str, priority: int, towns: list[str], title: str = "Verkäufer (m/w/d)"):
    return [(source, priority, title, "Markt GmbH", town) for town in towns]


# --- one posting is one job at one place -------------------------------------


def test_the_better_source_keeps_every_shop() -> None:
    """One title in three towns is three openings. The old pass kept one row
    for the whole employer, title and country."""
    rows = _shops("workday", 1, ["Lyon, France", "Paris, France", "Nice, France"])
    rows += _shops("welcometothejungle", 3, ["Paris, France"])

    assert _survivors(rows) == {"workday": 3}


def test_a_second_source_is_dropped_only_as_far_as_the_first_covers_it() -> None:
    rows = _shops("workday", 1, ["Lyon, FR", "Paris, FR"])
    rows += _shops("welcometothejungle", 3, ["Lyon, France", "Paris, France", "Nice, France"])

    # Lyon and Paris are the ATS rows again; Nice has no ATS row left to be.
    assert _survivors(rows) == {"workday": 2, "welcometothejungle": 1}


def test_a_board_that_gives_the_head_office_is_matched_by_count() -> None:
    """Same employer, title and country, another city: the board row is the
    ATS row as long as the ATS has one to spare."""
    rows = _shops("successfactors", 1, ["Canéjan, FR", "Thouars, FR"], "Ambulancier F/H")
    rows += _shops("welcometothejungle", 3, ["Courbevoie, France"] * 3, "Ambulancier F/H")

    assert _survivors(rows) == {"successfactors": 2, "welcometothejungle": 1}


def test_a_region_code_matches_any_town_once() -> None:
    """EURES gives a region code, Bundesagentur a town: two EURES rows can
    be two of the three Bundesagentur rows, not all of them."""
    rows = _shops("bundesagentur", 6, ["90429 Nürnberg, Bayern, Deutschland",
                                       "80331 München, Bayern, Deutschland",
                                       "86150 Augsburg, Bayern, Deutschland"])
    rows += _shops("eures", 6, ["DE (DE254)", "DE (DE212)", "DE (DE271)", "DE (DE271)"])

    assert _survivors(rows) == {"bundesagentur": 3, "eures": 1}


def test_a_row_that_lists_two_places_absorbs_one_row_per_place() -> None:
    rows = [("greenhouse", 1, "Account Executive", "Acme", "Paris; Lyon")]
    rows += [("welcometothejungle", 3, "Account Executive", "Acme", town)
             for town in ("Paris, France", "Lyon, France", "Nice, France")]

    assert _survivors(rows) == {"greenhouse": 1, "welcometothejungle": 1}


def test_a_place_is_absorbed_once_however_many_places_the_row_lists() -> None:
    """Two board rows in Paris cannot use the Paris and the Lyon slot of one ATS row."""
    rows = [("greenhouse", 1, "Account Executive", "Acme", "Paris; Lyon")]
    rows += [("welcometothejungle", 3, "Account Executive", "Acme", town)
             for town in ("Paris, France", "Paris (HQ), France", "Lyon, France")]

    assert _survivors(rows) == {"greenhouse": 1, "welcometothejungle": 1}


def test_an_absorbed_row_hands_over_what_it_absorbed() -> None:
    """A covers one opening. B is A (typo), and B had already absorbed one of
    the two C rows: the second C row is an opening nobody else lists."""
    rows = [
        ("workday", 1, "Senior Backend Enginer", "Acme", "Berlin"),
        ("eightfold", 5, "Senior Backend Engineer", "Acme", "Berlin, Germany"),
        ("eures", 6, "Senior Backend Engineer (m/f/d)", "Acme", "Berlin, DE"),
        ("eures", 6, "Senior Backend Engineer (m/f/d)", "Acme", "10115 Berlin"),
    ]

    assert _survivors(rows) == {"workday": 1, "eures": 1}


def test_a_board_row_that_lists_two_places_fills_both() -> None:
    """The first board row already covers Lyon: the second one in Lyon is
    another opening."""
    rows = [
        ("greenhouse", 1, "Account Executive", "Acme", "Paris; Lyon"),
        ("welcometothejungle", 3, "Account Executive", "Acme", "Paris (HQ); Lyon (Office)"),
        ("welcometothejungle", 3, "Account Executive", "Acme", "Lyon, France"),
    ]

    assert _survivors(rows) == {"greenhouse": 1, "welcometothejungle": 1}


def test_the_earliest_kept_row_wins_among_the_cities_of_a_row() -> None:
    """A row at "Paris; Lyon" meets a Paris row and a Lyon row of a better
    source: it is the earlier of the two, whatever order a set yields."""
    rows = [
        ("workday", 1, "Senior Backend Engineer", "Acme", "Paris"),
        ("workday", 1, "Senior Backend Engineer", "Acme", "Lyon"),
        ("eightfold", 5, "Senior Backend Enginer", "Acme", "Lyon; Paris"),
        ("eightfold", 5, "Senior Backend Enginer", "Acme", "Paris (HQ)"),
    ]

    for _ in range(5):
        assert _survivors(rows) == {"workday": 2, "eightfold": 1}


def test_a_handed_over_row_keeps_its_place() -> None:
    """B (Lyon) had absorbed a Lyon row of C. When A ("Paris; Lyon") absorbs
    B, that row fills A's Lyon slot, so the second Lyon row of C stays."""
    rows = [
        ("workday", 1, "Senior Backend Enginer", "Acme", "Paris; Lyon"),
        ("eightfold", 5, "Senior Backend Engineer", "Acme", "Lyon"),
        ("eures", 6, "Senior Backend Engineer (m/f/d)", "Acme", "Lyon, FR"),
        ("eures", 6, "Senior Backend Engineeer", "Acme", "Lyon, France"),
    ]

    assert _survivors(rows) == {"workday": 1, "eures": 1}


def test_a_large_group_of_one_title_is_handled() -> None:
    """4,000 shops on the ATS, their 4,000 copies on a board, and 4,000
    region-code rows of a third source: matched city by city."""
    towns = [f"Ville{number:04d}" for number in range(4000)]
    rows = _shops("workday", 1, [f"{town}, FR" for town in towns])
    rows += _shops("welcometothejungle", 3, [f"{town}, France" for town in towns])
    rows += _shops("eures", 6, ["FR (FRK26)"] * 4000)

    assert _survivors(rows) == {"workday": 4000}


def test_rows_of_one_source_are_never_compared() -> None:
    rows = _shops("workday", 1, ["Paris, France", "Paris, France"])

    assert _survivors(rows) == {"workday": 2}


# --- places ------------------------------------------------------------------


def test_a_row_without_a_country_is_matched_on_the_city() -> None:
    """Half of the ATS rows carry no country. The old passes skipped them."""
    rows = [
        ("ashby", 1, "Procurement Manager", "Vertice", "Sydney"),
        ("welcometothejungle", 3, "Procurement Manager", "Vertice", "Sydney, Australia"),
    ]

    assert _survivors(rows) == {"ashby": 1}


def test_two_countries_are_two_postings() -> None:
    rows = [
        ("lever", 1, "Customer Support Officer", "Winamax", "Germany"),
        ("welcometothejungle", 3, "Customer Support Officer", "Winamax", "Paris, France"),
    ]

    assert _survivors(rows) == {"lever": 1, "welcometothejungle": 1}


def test_two_cities_without_a_country_are_two_postings() -> None:
    rows = [
        ("ashby", 1, "Store Manager", "Acme", "Lisbon"),
        ("wellfound", 3, "Store Manager", "Acme", "Porto"),
    ]

    assert _survivors(rows) == {"ashby": 1, "wellfound": 1}


def test_a_short_city_name_in_another_script_is_a_city() -> None:
    """``北京`` and ``上海`` are two characters and two cities, not "no city"."""
    rows = [
        ("beisen", 1, "Backend Engineer", "字节跳动", "北京"),
        ("wanted", 3, "Backend Enginer", "字节跳动", "上海"),
    ]

    assert _survivors(rows) == {"beisen": 1, "wanted": 1}


def test_the_structured_country_wins_over_the_location_text() -> None:
    rows = [
        ("workday", 1, "Analyst", "Acme", "Paris"),
        ("welcometothejungle", 3, "Analyst", "Acme", "Paris (HQ)"),
    ]

    assert _survivors(rows) == {"workday": 1}
    # Paris, France and Paris, Texas: the scrapers say so, the text does not.
    keys = pl.DataFrame(
        [
            {"_local_idx": 0, "_orig_idx": 0, "_priority": 1, "ats_type": "workday",
             "url": "https://w.example/1", "title_raw": "Analyst", "title": "analyst",
             "company": "acme", "location": "paris", "ats_id": "w1", "country_iso": "FR"},
            {"_local_idx": 0, "_orig_idx": 1, "_priority": 3, "ats_type": "welcometothejungle",
             "url": "https://j.example/1", "title_raw": "Analyst", "title": "analyst",
             "company": "acme", "location": "paris (hq)", "ats_id": "j1", "country_iso": "US"},
        ]
    )
    kept = _decide_dedup_survivors_polars(keys)
    assert {source: frame.height for source, frame in kept.items()} == {
        "workday": 1,
        "welcometothejungle": 1,
    }


def test_what_pass_5_absorbed_still_counts_in_pass_6() -> None:
    """One ATS row cannot be two board rows, one per pass."""
    rows = [
        ("workday", 1, "Senior Backend Engineer", "Acme", "Berlin"),
        ("welcometothejungle", 3, "Senior Backend Engineer", "Acme", "Berlin, Germany"),
        ("welcometothejungle", 3, "Senior Backend Enginer", "Acme", "Berlin, Germany"),
    ]

    assert _survivors(rows) == {"workday": 1, "welcometothejungle": 1}


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("San Francisco, CA", ("san francisco",)),
        ("US, San Diego, CA", ("san diego",)),
        ("Hybrid - San Francisco, CA", ("san francisco",)),
        ("90429 Nürnberg, Mittelfranken, Bayern, Deutschland", ("nurnberg",)),
        ("Julius-Faucher-Straße 2, 28307 Bremen, Bremen, Deutschland", ("bremen",)),
        ("Paris 75008, France", ("paris",)),
        ("Ébène, Mauritius", ("ebene",)),
        ("Remote - California; Remote - Colorado", ("california", "colorado")),
        ("Zürich | Bern", ("zurich", "bern")),
        ("DE (DEC04)", ("",)),
        ("Anywhere in the world", ("",)),
        ("Remote - UK", ("",)),
        ("United States", ("",)),
        ("北京", ("北京",)),
        ("서울", ("서울",)),
        ("NY", ("",)),
        ("", ("",)),
        (None, ("",)),
    ],
)
def test_the_city_of_a_place(location, expected) -> None:
    assert place_slots(location) == expected


def test_place_slots() -> None:
    assert place_slots("Paris; Lyon; Nice") == ("paris", "lyon", "nice")
    assert place_slots("Zürich | Bern") == ("zurich", "bern")
    assert place_slots("Paris, France") == ("paris",)
    # A city listed twice is one slot; a place without a city is an unnamed one.
    assert place_slots("Paris; Paris, France") == ("paris",)
    assert place_slots("Paris; Remote") == ("paris", "")
    assert place_slots("DE (DEA22)") == ("",)
    assert place_slots("") == ("",)
    assert place_slots(None) == ("",)


def test_a_city_listed_twice_is_one_place() -> None:
    """``"Paris; Paris, France"`` names Paris only: a near-title row in Nice
    has no slot to take."""
    rows = [
        ("workday", 1, "Senior Backend Engineer", "Acme", "Paris; Paris, France"),
        ("eightfold", 5, "Senior Backend Enginer", "Acme", "Nice, France"),
    ]

    assert _survivors(rows) == {"workday": 1, "eightfold": 1}


# --- titles ------------------------------------------------------------------


def test_punctuation_gender_tags_and_accents_do_not_make_two_titles() -> None:
    rows = [
        ("greenhouse", 1, "Account Manager (Gaming)", "Fandom", "London"),
        ("welcometothejungle", 3, "Account Manager, Gaming", "Fandom", "London, United Kingdom"),
        ("greenhouse", 1, "Tech Lead (m/f/d)", "Fandom", "Berlin"),
        ("welcometothejungle", 3, "Tech Lead", "Fandom", "Berlin, Germany"),
        ("greenhouse", 1, "Ingénieur méthodes (H/F)", "Fandom", "Pau"),
        ("welcometothejungle", 3, "Ingenieur Méthodes F/H", "Fandom", "Pau, France"),
    ]

    assert _survivors(rows) == {"greenhouse": 3}


def test_a_closing_bracket_is_a_tag_on_the_plain_title() -> None:
    """EURES appends the occupation, sometimes with a bracket inside it."""
    rows = [
        ("bundesagentur", 6, "Wohnbereichsleitung (m/w/d)", "Pflege GmbH", "Bonn, Deutschland"),
        ("eures", 6, "Wohnbereichsleitung (m/w/d) (Pflegefachmann/-frau (Altenpflege))",
         "Pflege GmbH", "DE (DEA22)"),
        ("bundesagentur", 6, "Reinigungskraft (m/w/d) - Teilzeit (20 Std./Woche)", "Pflege GmbH",
         "Bonn, Deutschland"),
        ("eures", 6, "Reinigungskraft (m/w/d) - Teilzeit (20 Std./Woche) (Helfer/in - Reinigung)",
         "Pflege GmbH", "DE (DEA22)"),
    ]

    assert sum(_survivors(rows).values()) == 2


def test_a_title_is_matched_through_every_bracket() -> None:
    """``X``, ``X (hours)`` and ``X (hours) (occupation)`` in one employer:
    the last is the second, whatever group the plain ``X`` opens."""
    rows = [
        ("softgarden", 1, "Reinigungskraft - Teilzeit", "Pflege GmbH", "Bonn, Deutschland"),
        ("softgarden", 1, "Reinigungskraft - Teilzeit (20 Std./Woche)", "Pflege GmbH",
         "Bonn, Deutschland"),
        ("eures", 6, "Reinigungskraft - Teilzeit (20 Std./Woche) (Helfer/in - Reinigung)",
         "Pflege GmbH", "Köln, Deutschland"),
    ]

    assert _survivors(rows) == {"softgarden": 2}


def test_two_different_brackets_are_two_jobs() -> None:
    rows = [
        ("ashby", 1, "Software Engineer (Backend)", "Acme", "Paris"),
        ("welcometothejungle", 3, "Software Engineer (Frontend)", "Acme", "Paris, France"),
    ]

    assert _survivors(rows) == {"ashby": 1, "welcometothejungle": 1}


def test_punctuation_that_names_a_language_is_kept() -> None:
    rows = [
        ("greenhouse", 1, "C++ Developer", "Acme", "Paris"),
        ("welcometothejungle", 3, "C# Developer", "Acme", "Paris, France"),
        ("greenhouse", 1, ".NET Engineer", "Acme", "Paris"),
        ("welcometothejungle", 3, "Net Engineer", "Acme", "Paris, France"),
    ]

    assert _survivors(rows) == {"greenhouse": 2, "welcometothejungle": 2}


def test_a_title_inside_a_longer_title_is_another_job() -> None:
    """``token_set_ratio`` scored these 100: one title's words are all in
    the other. They are different jobs."""
    rows = [
        ("lever", 1, "Consultant", "Reply", "Milan"),
        ("successfactors", 1, "AI Data and Emerging Tech Consultant", "Reply", "Milan"),
        ("phenom", 1, "Senior Solution Architect - Content Supply Chain", "Reply", "Milan"),
        ("smartrecruiters", 1, "Solution Architect", "Reply", "Milan"),
    ]

    assert sum(_survivors(rows).values()) == 4


def test_a_typo_is_the_same_job_only_where_the_places_agree() -> None:
    same_place = [
        ("workday", 1, "Senior Backend Engineer", "Acme", "Berlin, Germany"),
        ("eightfold", 5, "Senior Backend Enginer", "Acme", "Berlin, Germany"),
    ]
    other_city = [
        ("workday", 1, "Senior Backend Engineer", "Acme", "Berlin, Germany"),
        ("eightfold", 5, "Senior Backend Enginer", "Acme", "Munich, Germany"),
    ]

    assert _survivors(same_place) == {"workday": 1}
    assert _survivors(other_city) == {"workday": 1, "eightfold": 1}


@pytest.mark.parametrize(
    ("title", "key", "base"),
    [
        ("Backend Engineer (m/w/d)", "backend engineer", "backend engineer"),
        ("HÔTE DE CAISSE (H/F)", "hote de caisse", "hote de caisse"),
        ("Gesundheits- und Kinderkrankenpfleger:in w/m/d",
         "gesundheits und kinderkrankenpfleger in", "gesundheits und kinderkrankenpfleger in"),
        ("Senior Engineer (Payments)", "senior engineer payments", "senior engineer"),
        ("Front Office Agent (all genders) (Empfangsmitarbeiter/in (Hotel))",
         "front office agent empfangsmitarbeiter in hotel", "front office agent"),
        ("R&D Engineer F/H", "r d engineer", "r d engineer"),
        ("C/C++ Developer", "c cpp developer", "c cpp developer"),
        ("C# / F# Engineer (.NET)", "csharp fsharp engineer dotnet", "csharp fsharp engineer"),
        ("", "", ""),
        (None, "", ""),
    ],
)
def test_title_key_and_base(title, key, base) -> None:
    assert title_key(title) == key
    assert title_base(title) == base


# --- employers ---------------------------------------------------------------


def test_an_employer_in_another_script_is_compared_too() -> None:
    """The old key kept ``a-z0-9`` only: a Japanese or Korean name gave an
    empty key and the row skipped every title pass."""
    rows = [
        ("greenhouse", 1, "Backend Engineer", "株式会社メルカリ", "Tokyo"),
        ("wantedly", 3, "Backend Engineer", "株式会社メルカリ", "Tokyo"),
    ]

    assert _survivors(rows) == {"greenhouse": 1}


def test_a_placeholder_employer_matches_nobody() -> None:
    rows = [
        ("builtin", 3, "Data Analyst", "Unknown", "Chicago"),
        ("remoteok", 3, "Data Analyst", "Unknown", "Chicago, IL"),
    ]

    assert _survivors(rows) == {"builtin": 1, "remoteok": 1}


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("L'Oréal", "loreal"),
        ("L'Oreal", "loreal"),
        ("Stripe, Inc.", "stripeinc"),
        ("株式会社メルカリ", "株式会社メルカリ"),
        ("엔비디아", "엔비디아"),
        ("がんばる株式会社", "がんばる株式会社"),
        ("Unknown", ""),
        ("Confidential", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_company_key(company, expected) -> None:
    assert company_key(company) == expected
