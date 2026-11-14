"""Title passes of the cross-source dedup: which rows of two sources are one posting.

The rule, in one line: one posting is one job at one place, so a kept row
absorbs, in each other source, at most one row per place it lists.

Two rows of different sources can be the same posting when

* the employer is the same (:func:`company_key`),
* the title is the same or nearly so (:func:`title_key`, :func:`title_base`),
* the places do not contradict each other: the same country when both rows
  give one, the same city when both name one (:func:`place_slots`).

Rows of one source are never compared with each other, and the source with
the better priority keeps all of its rows. The limit counts what the title
passes absorb; a row removed earlier by an exact pass (same URL, same
employer + title + location text, same requisition id) does not use it up.

Why the limit of one row per place. A retailer that advertises "Verkäufer
(m/w/d)" for 224 shops has 224 openings. Collapsing every row that shares
employer, title and country into one keeps a single shop. Counting instead
(one absorbed row per kept row and per place) drops a second source only as
far as the first one really covers it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

# How well the places of two rows agree. Higher is better; 0 means "not the
# same posting".
_SAME_CITY = 3
_CITY_UNKNOWN = 2  # one of the two rows names no city we can compare
_OTHER_CITY = 1  # both name a city, they differ, the country is the same


def _fold(text: str) -> str:
    """Drop accents so ``Ébène`` and ``Ebene`` compare equal.

    Only the Latin-style combining marks (U+0300 to U+036F) are removed and
    the text is recomposed, so Hangul syllables and Japanese voiced kana
    come back unchanged.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return unicodedata.normalize(
        "NFC", "".join(ch for ch in decomposed if not "\u0300" <= ch <= "\u036f")
    )


# --- employer ----------------------------------------------------------------

# Names that stand for "we do not know the employer". They must not put
# unrelated rows into one employer block.
_PLACEHOLDER_EMPLOYERS = frozenset(
    {
        "unknown",
        "confidential",
        "confidentiel",
        "anonymous",
        "anonym",
        "na",
        "none",
        "null",
        "company",
        "stealth",
        "stealthstartup",
        "undisclosed",
        "notspecified",
        "private",
    }
)


def company_key(company: object) -> str:
    """Employer key: the letters and digits of the name, any script, no accents.

    Returns ``""`` for an empty or placeholder name, which keeps the row out
    of the title passes.
    """
    if not isinstance(company, str):
        return ""
    key = "".join(ch for ch in _fold(company.lower()) if ch.isalnum())
    return "" if key in _PLACEHOLDER_EMPLOYERS else key


# --- title -------------------------------------------------------------------

# "(m/w/d)", "h/f", "f/m/x", "w/m/div", "(all genders)", "mwd": the tag says
# nothing about which job it is.
_GENDER_TAG_RE = re.compile(
    r"\(?(?<![a-z0-9])"
    r"(?:[mwfhd]\s*[/|]\s*[mwfhdx](?:\s*[/|]\s*(?:[mwfhdx]|div|divers|nb))?"
    r"|all genders|alle geschlechter|gn|mwd|wmd|mfd|fmd)"
    r"(?![a-z0-9])\*?\)?"
)
_NOT_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


# Punctuation that names a language: "C++ Developer" is not "C# Developer".
_SPELLED_OUT = (
    (re.compile(r"(?<![a-z0-9])c\+\+"), " cpp "),
    (re.compile(r"(?<![a-z0-9])c#"), " csharp "),
    (re.compile(r"(?<![a-z0-9])f#"), " fsharp "),
    (re.compile(r"(?<![a-z0-9])\.net(?![a-z0-9])"), " dotnet "),
)


def _tokens(text: str) -> str:
    lowered = _fold(text.lower())
    for pattern, word in _SPELLED_OUT:
        lowered = pattern.sub(word, lowered)
    cleaned = _GENDER_TAG_RE.sub(" ", lowered)
    return " ".join(_NOT_WORD_RE.sub(" ", cleaned).replace("_", " ").split())


def _strip_trailing_bracket(title: str) -> str:
    """Remove a bracket that closes the title, nested ones included.

    EURES appends the occupation to the title, sometimes with a bracket of
    its own: ``"Wohnbereichsleitung (m/w/d) (Pflegefachmann/-frau
    (Altenpflege))"``.
    """
    text = title.rstrip()
    if not text.endswith(")"):
        return title
    depth = 0
    for pos in range(len(text) - 1, -1, -1):
        if text[pos] == ")":
            depth += 1
        elif text[pos] == "(":
            depth -= 1
            if depth == 0:
                return text[:pos].rstrip()
    return title


def title_key(title: object) -> str:
    """Title without case, accents, punctuation and gender tags."""
    if not isinstance(title, str) or not title.strip():
        return ""
    return _tokens(title)


def title_base(title: object) -> str:
    """:func:`title_key` of the title without its closing bracket.

    ``"Senior Engineer (Payments)"`` gives ``"senior engineer"``. A base is
    only ever compared with the *full* key of another row: ``"X (tag)"`` is
    ``"X"``, but ``"X (a)"`` is not ``"X (b)"``.
    """
    if not isinstance(title, str) or not title.strip():
        return ""
    return _tokens(_strip_trailing_bracket(title))


# --- place -------------------------------------------------------------------

_NUTS_RE = re.compile(r"^\s*[a-z]{2}\s*\(")  # EURES: "de (dec04)" names a region code
_PLACE_SPLIT_RE = re.compile(r"\s*[;|]\s*")
_BRACKET_RE = re.compile(r"\([^()]*\)")
_LEADING_CODE_RE = re.compile(r"^[\d\s-]+")
_TRAILING_CODE_RE = re.compile(r"\s+\d{4,6}$")
_POSTCODE_TOWN_RE = re.compile(r"(?:^|,)\s*\d{4,5}\s+([^\d,]{3,})")
_WORK_MODE_RE = re.compile(
    r"^(?:remote|hybrid|hybride|onsite|on-site|on site|télétravail|teletravail|home ?office)\b"
    r"[\s:–—-]*"
)
_NOT_A_CITY = frozenset(
    {
        "remote",
        "hybrid",
        "anywhere",
        "anywhere in the world",
        "worldwide",
        "global",
        "multiple locations",
        "various",
        "various locations",
        "mehrere standorte",
        "emea",
        "apac",
        "latam",
        "europe",
        "asia",
        "north america",
        "americas",
        "us",
        "usa",
        "uk",
        "eu",
        "nationwide",
        "bundesweit",
        "deutschlandweit",
        "home office",
        "homeoffice",
        "télétravail",
        "teletravail",
        "full remote",
        "fully remote",
        "field",
        "n/a",
    }
)
_COUNTRY_WORDS = frozenset(
    {
        "deutschland", "germany", "allemagne", "france", "frankreich", "belgique",
        "belgium", "belgien", "belgië", "österreich", "austria", "autriche",
        "nederland", "netherlands", "pays-bas", "niederlande", "italia", "italy",
        "italien", "españa", "spain", "espagne", "spanien", "portugal", "polska",
        "poland", "pologne", "polen", "schweiz", "suisse", "switzerland", "svizzera",
        "luxembourg", "luxemburg", "danmark", "denmark", "dänemark", "sverige",
        "sweden", "schweden", "norge", "norway", "norwegen", "suomi", "finland",
        "finnland", "ireland", "irlande", "irland", "česko", "czech", "tschechien",
        "united states", "united states of america", "u.s.a", "united kingdom",
        "england", "scotland", "wales", "canada", "india", "japan", "china",
        "brasil", "brazil", "australia", "singapore", "mexico", "méxico", "korea",
        "south korea", "israel", "turkey", "türkiye", "romania", "hungary", "greece",
        "argentina", "colombia", "chile", "peru", "philippines", "indonesia",
        "malaysia", "thailand", "vietnam", "taiwan", "hong kong", "new zealand",
        "south africa", "nigeria", "kenya", "egypt", "uae", "united arab emirates",
        "saudi arabia", "qatar", "ukraine", "bulgaria", "croatia", "serbia",
        "slovakia", "slovenia", "estonia", "latvia", "lithuania", "iceland",
        "pakistan", "bangladesh",
    }
)  # fmt: skip


def _usable(piece: str) -> bool:
    # Two ASCII letters are a state or country code ("ca", "uk"); two
    # characters of another script are a city ("北京", "서울").
    return (
        (len(piece) >= 3 or not piece.isascii())
        and piece not in _NOT_A_CITY
        and piece not in _COUNTRY_WORDS
        and any(ch.isalpha() for ch in piece)
    )


def _city_of(place: str) -> str:
    place = _WORK_MODE_RE.sub("", _BRACKET_RE.sub(" ", place).strip()).strip()
    # A town after a postcode wins over a street in front of it:
    # "julius-faucher-straße 2, 28307 bremen, bremen, deutschland".
    after_postcode = _POSTCODE_TOWN_RE.search(place)
    if after_postcode:
        town = " ".join(after_postcode.group(1).strip(" .-").split())
        if _usable(town):
            return _fold(town)
    for piece in place.split(","):
        piece = _TRAILING_CODE_RE.sub("", _LEADING_CODE_RE.sub("", piece))
        piece = " ".join(piece.strip(" .-").split())
        if _usable(piece):
            return _fold(piece)
    return ""


def _places(location: str) -> list[str]:
    return [part for part in _PLACE_SPLIT_RE.split(location) if part.strip()]


def place_slots(location: object) -> tuple[str, ...]:
    """One slot per place a row lists: the city it names, or ``""``.

    Only the first usable piece of each place is read (``"san francisco,
    ca"`` gives ``san francisco``), so a region, a postcode or a country
    after it never makes two spellings of one city look different.

    A city listed twice (``"Paris; Paris, France"``) is one slot. A place
    without a city we can compare (``"Remote"``, a country, a EURES region
    code) is an unnamed slot, which agrees with any city. Always at least
    one slot.
    """
    if not isinstance(location, str) or not location.strip():
        return ("",)
    lowered = location.lower()
    if _NUTS_RE.match(lowered):
        return ("",)
    named: list[str] = []
    unnamed = 0
    for place in _places(lowered):
        city = _city_of(place)
        if not city:
            unnamed += 1
        elif city not in named:
            named.append(city)
    return (*named, *[""] * unnamed) or ("",)


# --- matching ----------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    idx: int
    source: int  # rank of the source: lower wins
    country: str
    cities: frozenset[str]
    # One slot per listed place: the city it names, or "" when it names none.
    slots: tuple[str, ...]
    key: str
    base: str
    # source rank -> {position in ``slots``: the row of that source it stands for}
    used: dict[int, dict[int, _Row]] = field(default_factory=dict)


def _free_slot(
    row: _Row,
    kept: _Row,
    taken: dict[int, _Row] | set[int],
    *,
    other_city_ok: bool,
) -> tuple[int, int]:
    """Best slot of ``kept`` for ``row`` outside ``taken``: ``(agreement, position)``.

    ``taken`` holds the positions rows of ``row``'s source already fill.
    Returns ``(0, -1)`` when no free slot agrees. A row in Paris takes the
    Paris slot of a kept ``"Paris; Lyon"`` row, never the Lyon one twice.
    """
    if row.country and kept.country and row.country != kept.country:
        return 0, -1
    best, best_position = 0, -1
    for position, city in enumerate(kept.slots):
        if position in taken:
            continue
        if city and row.cities:
            if city in row.cities:
                return _SAME_CITY, position
            agreement = _OTHER_CITY if other_city_ok and row.country and kept.country else 0
        else:
            agreement = _CITY_UNKNOWN
        if agreement > best:
            best, best_position = agreement, position
    return best, best_position


def _claim(
    row: _Row,
    kept: _Row,
    taken: dict[int, _Row] | set[int],
    *,
    other_city_ok: bool,
) -> tuple[int, list[int]]:
    """Slots of ``kept`` that ``row`` fills: ``(agreement, positions)``, ``(0, [])`` when none.

    The first position is the best free slot. A row that lists several
    places also fills every other free slot naming one of its cities: a
    board row at ``"Paris; Lyon"`` covers both places of a kept ``"Paris;
    Lyon"`` row, so a second board row in Lyon is another opening.
    """
    agreement, position = _free_slot(row, kept, taken, other_city_ok=other_city_ok)
    if not agreement:
        return 0, []
    positions = [position]
    if len(row.cities) > 1:
        positions += [
            other
            for other, city in enumerate(kept.slots)
            if other != position and other not in taken and city and city in row.cities
        ]
    return agreement, positions


def _hand_over_plan(row: _Row, kept: _Row) -> list[tuple[int, int, _Row]] | None:
    """Where the rows ``row`` stands for would go if ``kept`` absorbed it.

    Each of them needs a free slot of ``kept`` whose place agrees with its
    own, exactly as if ``kept`` had met it directly. Returns
    ``(source, position, row)`` triples, or ``None`` when one of them finds
    no slot: ``kept`` then cannot stand for ``row``.
    """
    plan: list[tuple[int, int, _Row]] = []
    for source, absorbed in row.used.items():
        taken = set(kept.used.get(source, ()))
        # A row that filled several slots is one row to hand over.
        for earlier in {id(member): member for member in absorbed.values()}.values():
            agreement, positions = _claim(earlier, kept, taken, other_city_ok=True)
            if not agreement:
                return None
            taken.update(positions)
            plan.extend((source, position, earlier) for position in positions)
    return plan


def _is_full(kept: _Row, source: int) -> bool:
    return len(kept.used.get(source, ())) >= len(kept.slots)


def _has_free_city(kept: _Row, source: int, city: str) -> bool:
    taken = kept.used.get(source, ())
    return any(
        name == city and position not in taken for position, name in enumerate(kept.slots)
    )


def absorb(
    rows: Iterable[_Row],
    same_title: Callable[[_Row, _Row], bool],
    *,
    other_city_ok: bool,
) -> list[int]:
    """Indexes to drop among ``rows`` (one employer, sorted by source rank then index).

    Each row is kept unless an already kept row of a *better* source has the
    same title and a free slot whose place agrees. The best agreeing place
    wins (same city, then unknown city, then another city of the same
    country when ``other_city_ok``). A row that absorbed others in an
    earlier pass hands them over, each to a slot that agrees with its own
    place, and is only absorbed where they all fit: one kept row never
    stands for more postings of a source than it lists places.

    Candidates are indexed per source in hand: by the cities they still have
    a free slot for, and as a list of those not yet full. A shop chain with
    thousands of rows per source is then matched city by city instead of
    scanning every kept row for every incoming one.
    """
    kept: list[_Row] = []  # rows of the sources already gone through
    current: list[_Row] = []  # kept rows of the source in hand: never candidates for it
    source: int | None = None
    by_city: dict[str, dict[int, _Row]] = {}
    not_full: dict[int, _Row] = {}
    dropped: list[int] = []

    def take(row: _Row, candidate: _Row, positions: list[int], plan: list) -> None:
        for position in positions:
            candidate.used.setdefault(row.source, {})[position] = row
        for other, position, earlier in plan:
            candidate.used.setdefault(other, {})[position] = earlier
        row.used = {}
        dropped.append(row.idx)

    for row in rows:
        if row.source != source:
            kept.extend(current)
            current, source = [], row.source
            not_full = {id(c): c for c in kept if not _is_full(c, source)}
            by_city = {}
            for candidate in not_full.values():
                for city in candidate.cities:
                    if _has_free_city(candidate, source, city):
                        by_city.setdefault(city, {})[id(candidate)] = candidate

        # Same city first: nothing agrees better. Among the row's cities (in
        # the order it lists them) the earliest kept row wins, so the result
        # does not depend on how a set happens to be ordered.
        same_city: tuple[tuple[int, int], _Row, list[int], list] | None = None
        for city in dict.fromkeys(name for name in row.slots if name):
            for ident, candidate in list(by_city.get(city, {}).items()):
                if not _has_free_city(candidate, source, city):
                    del by_city[city][ident]
                    continue
                agreement, positions = _claim(
                    row, candidate, candidate.used.get(source, ()), other_city_ok=other_city_ok
                )
                if agreement != _SAME_CITY or not same_title(row, candidate):
                    continue
                plan = _hand_over_plan(row, candidate)
                if plan is None:
                    continue
                rank = (candidate.source, candidate.idx)
                if same_city is None or rank < same_city[0]:
                    same_city = (rank, candidate, positions, plan)
                break  # the earliest fitting row for this city
        if same_city is not None:
            take(row, same_city[1], same_city[2], same_city[3])
            continue

        # No slot in the same city: an unnamed slot, or another city of the same country.
        best: _Row | None = None
        best_agreement = 0
        best_positions: list[int] = []
        best_plan: list[tuple[int, int, _Row]] = []
        for ident, candidate in list(not_full.items()):
            if _is_full(candidate, source):
                del not_full[ident]
                continue
            agreement, positions = _claim(
                row, candidate, candidate.used.get(source, ()), other_city_ok=other_city_ok
            )
            if agreement <= best_agreement or not same_title(row, candidate):
                continue
            plan = _hand_over_plan(row, candidate)
            if plan is None:
                continue
            best, best_agreement, best_positions, best_plan = candidate, agreement, positions, plan
            if agreement == _CITY_UNKNOWN:
                break  # the best this scan can find
        if best is None:
            current.append(row)
        else:
            take(row, best, best_positions, best_plan)
    return dropped


def _same_title(a: _Row, b: _Row) -> bool:
    return a.key == b.key or a.base == b.key or a.key == b.base


def _near_title(threshold: int) -> Callable[[_Row, _Row], bool]:
    from rapidfuzz import fuzz

    def near(a: _Row, b: _Row) -> bool:
        # token_sort_ratio ignores word order but gives no credit for one
        # title being a subset of the other: "Consultant" is not "AI Data
        # and Emerging Tech Consultant".
        if fuzz.token_sort_ratio(a.key, b.key) >= threshold:
            return True
        if a.base != a.key and fuzz.token_sort_ratio(a.base, b.key) >= threshold:
            return True
        return b.base != b.key and fuzz.token_sort_ratio(a.key, b.base) >= threshold

    return near


_ROW_COLUMNS = ("_orig_idx", "_source", "_country_iso", "_slots", "_tkey", "_tbase")


def _rows(frame: pl.DataFrame) -> list[_Row]:
    columns: Sequence[list] = [frame[name].to_list() for name in _ROW_COLUMNS]
    rows: list[_Row] = []
    for idx, source, country, slot_list, key, base in zip(*columns, strict=True):
        slots = tuple(slot_list.split("|"))
        rows.append(
            _Row(
                idx=idx,
                source=source,
                country=country,
                cities=frozenset(city for city in slots if city),
                slots=slots,
                key=key,
                base=base,
            )
        )
    return rows


def _prepare(work: pl.DataFrame) -> pl.DataFrame:
    """Rows that can hold a cross-source duplicate, with their match columns.

    Each distinct employer, title and location is normalised once, then
    joined back: the corpus has millions of rows but far fewer distinct
    strings.
    """
    rows = work.select(
        "_orig_idx",
        "_priority",
        "ats_type",
        pl.col("title_raw").fill_null(""),
        pl.col("company").fill_null(""),
        pl.col("location").fill_null(""),
        pl.col("_country_iso").fill_null(""),
    )
    employers = rows.select("company").unique().with_columns(
        pl.col("company").map_elements(company_key, return_dtype=pl.String).alias("_employer")
    )
    rows = rows.join(employers, on="company", how="left").drop("company")
    # Only an employer seen in two sources or more can hold a cross-source duplicate.
    rows = rows.filter(pl.col("_employer") != "").filter(
        pl.col("ats_type").n_unique().over("_employer") > 1
    )
    if rows.is_empty():
        return rows

    ordered = rows.sort(["_priority", "_orig_idx"])["ats_type"].unique(maintain_order=True)
    rank = {source: position for position, source in enumerate(ordered.to_list())}
    titles = rows.select("title_raw").unique().with_columns(
        pl.col("title_raw").map_elements(title_key, return_dtype=pl.String).alias("_tkey"),
        pl.col("title_raw").map_elements(title_base, return_dtype=pl.String).alias("_tbase"),
    )
    locations = rows.select("location").unique().with_columns(
        pl.col("location")
        .map_elements(lambda value: "|".join(place_slots(value)), return_dtype=pl.String)
        .alias("_slots"),
    )
    rows = (
        rows.join(titles, on="title_raw", how="left")
        .join(locations, on="location", how="left")
        .with_columns(
            pl.col("ats_type").replace_strict(rank, return_dtype=pl.Int32).alias("_source")
        )
        .drop("title_raw", "location", "_priority")
        .filter(pl.col("_tkey") != "")
    )
    return rows


def _title_groups(members: list[_Row]) -> dict[str, list[_Row]]:
    """Rows of one employer grouped for pass 5.

    ``"X (tag)"`` joins the group of a plain ``"X"`` when the employer has
    one, and so on through every bracket: ``"X (hours) (occupation)"``,
    ``"X (hours)"`` and ``"X"`` share a group. :func:`_same_title` then
    decides pair by pair. Without a plain ``"X"``, ``"X (a)"`` and
    ``"X (b)"`` stay in groups of their own.
    """
    keys = {row.key for row in members}
    plain_of: dict[str, str] = {}
    for row in members:
        if row.base and row.base != row.key and row.base in keys:
            known = plain_of.get(row.key)
            if known is None or len(row.base) < len(known):
                plain_of[row.key] = row.base
    groups: dict[str, list[_Row]] = {}
    for row in members:
        root = row.key
        while root in plain_of:  # a base is always shorter than its key
            root = plain_of[root]
        groups.setdefault(root, []).append(row)
    return groups


def _employer_drops(
    members: list[_Row],
    near_title: Callable[[_Row, _Row], bool],
    *,
    employer: str,
    max_block_size: int,
) -> set[int]:
    """Both title passes for the rows of one employer (sorted by source, then index).

    The passes share the rows, so what a kept row absorbed in pass 5 still
    counts against its limit in pass 6.
    """
    dropped: set[int] = set()

    # ---- Pass 5: same title -------------------------------------------------
    for group in _title_groups(members).values():
        if len({row.source for row in group}) > 1:
            dropped.update(absorb(group, _same_title, other_city_ok=True))

    # ---- Pass 6: nearly the same title --------------------------------------
    left = [row for row in members if row.idx not in dropped]
    if len({row.source for row in left}) < 2:
        return dropped
    if len(left) <= max_block_size:
        dropped.update(absorb(left, near_title, other_city_ok=False))
        return dropped
    by_country: dict[str, list[_Row]] = {}
    for row in left:
        if row.country:
            by_country.setdefault(row.country, []).append(row)
    for country, block in by_country.items():
        if len({row.source for row in block}) < 2:
            continue
        if len(block) > max_block_size:
            logger.warning(
                "Phase-2 fuzzy dedup: skipping oversize block (%d rows, company=%s country=%s).",
                len(block),
                employer,
                country,
            )
            continue
        dropped.update(absorb(block, near_title, other_city_ok=False))
    return dropped


def title_pass_drops(
    work: pl.DataFrame,
    *,
    fuzzy_threshold: int,
    fuzzy_max_block_size: int,
) -> set[int]:
    """``_orig_idx`` of the rows the two title passes remove.

    ``work`` needs ``_orig_idx``, ``_priority``, ``ats_type``, ``title_raw``,
    ``company``, ``location`` and ``_country_iso`` (``""`` when unknown).

    Pass 5 groups the rows of an employer by title. Pass 6 compares the
    titles of what is left with rapidfuzz, and asks the places to agree
    (same city, or one side naming none). An employer with more rows than
    ``fuzzy_max_block_size`` is compared country by country in pass 6; a
    country block that is still too large is skipped with a warning.
    """
    rows = _prepare(work)
    if rows.is_empty():
        return set()
    near_title = _near_title(fuzzy_threshold)
    dropped: set[int] = set()
    blocks = rows.sort(["_source", "_orig_idx"]).group_by("_employer", maintain_order=True)
    for (employer,), block in blocks:
        dropped.update(
            _employer_drops(
                _rows(block),
                near_title,
                employer=str(employer),
                max_block_size=fuzzy_max_block_size,
            )
        )
    return dropped
