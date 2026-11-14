"""Title passes of the cross-source dedup: which rows of two sources are one posting.

The rule, in one line: one posting is one job at one place, so a kept row
absorbs, in each other source, at most one row per place it lists.

Two rows of different sources can be the same posting when

* the employer is the same (:func:`company_key`),
* the title is the same or nearly so (:func:`title_key`, :func:`title_base`),
* the places do not contradict each other: the same country when both rows
  give one, the same city when both name one (:func:`cities`).

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
    """Drop accents so ``Ébène`` and ``Ebene`` compare equal."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
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


def _tokens(text: str) -> str:
    cleaned = _GENDER_TAG_RE.sub(" ", _fold(text.lower()))
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
    return (
        len(piece) >= 3
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


def cities(location: object) -> tuple[str, ...]:
    """Cities a location names, one per listed place.

    Only the first usable piece of each place is read (``"san francisco,
    ca"`` gives ``san francisco``), so a region, a postcode or a country
    after it never makes two spellings of one city look different. Returns
    ``()`` when the location names no city we can compare: empty, a country
    alone, "remote", a EURES region code.
    """
    if not isinstance(location, str) or not location.strip():
        return ()
    lowered = location.lower()
    if _NUTS_RE.match(lowered):
        return ()
    found: list[str] = []
    for place in _places(lowered):
        city = _city_of(place)
        if city and city not in found:
            found.append(city)
    return tuple(found)


def place_count(location: object) -> int:
    """How many places a row lists (``"; "`` or ``" | "`` separated), at least one."""
    if not isinstance(location, str):
        return 1
    return max(1, len(_places(location)))


# --- matching ----------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    idx: int
    source: int  # rank of the source: lower wins
    country: str
    cities: frozenset[str]
    capacity: int
    key: str
    base: str
    group: str  # title group of pass 5
    used: dict[int, int] = field(default_factory=dict)  # source rank -> rows absorbed


def _place_agreement(row: _Row, kept: _Row, *, other_city_ok: bool) -> int:
    if row.country and kept.country and row.country != kept.country:
        return 0
    if row.cities and kept.cities:
        if not row.cities.isdisjoint(kept.cities):
            return _SAME_CITY
        return _OTHER_CITY if other_city_ok and row.country and kept.country else 0
    return _CITY_UNKNOWN


def absorb(
    rows: Iterable[_Row],
    same_title: Callable[[_Row, _Row], bool],
    *,
    other_city_ok: bool,
) -> list[int]:
    """Indexes to drop among ``rows`` (one employer, sorted by source rank then index).

    Each row is kept unless an already kept row of a *better* source has the
    same title, an agreeing place and room left for this row's source. The
    best agreeing place wins (same city, then unknown city, then another
    city of the same country when ``other_city_ok``).
    """
    kept: list[_Row] = []
    dropped: list[int] = []
    for row in rows:
        best: _Row | None = None
        best_agreement = 0
        for candidate in kept:
            if candidate.source == row.source:
                continue
            if candidate.used.get(row.source, 0) >= candidate.capacity:
                continue
            agreement = _place_agreement(row, candidate, other_city_ok=other_city_ok)
            if agreement <= best_agreement or not same_title(row, candidate):
                continue
            best, best_agreement = candidate, agreement
            if agreement == _SAME_CITY:
                break
        if best is None:
            kept.append(row)
        else:
            best.used[row.source] = best.used.get(row.source, 0) + 1
            dropped.append(row.idx)
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


_ROW_COLUMNS = (
    "_orig_idx", "_source", "_country_iso", "_cities", "_capacity", "_tkey", "_tbase", "_tgroup",
)  # fmt: skip


def _rows(frame: pl.DataFrame) -> list[_Row]:
    columns: Sequence[list] = [frame[name].to_list() for name in _ROW_COLUMNS]
    return [
        _Row(
            idx=idx,
            source=source,
            country=country,
            cities=frozenset(city_list.split("|")) if city_list else frozenset(),
            capacity=capacity,
            key=key,
            base=base,
            group=group,
        )
        for idx, source, country, city_list, capacity, key, base, group in zip(
            *columns, strict=True
        )
    ]


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
        .map_elements(lambda value: "|".join(cities(value)), return_dtype=pl.String)
        .alias("_cities"),
        pl.col("location").map_elements(place_count, return_dtype=pl.Int32).alias("_capacity"),
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
    # "X (tag)" joins the group of a plain "X" of the same employer. Without
    # a plain "X", "X (a)" and "X (b)" stay in groups of their own.
    plain = rows.select("_employer", pl.col("_tkey").alias("_tbase")).unique().with_columns(
        pl.lit(True).alias("_plain_exists")
    )
    return rows.join(plain, on=["_employer", "_tbase"], how="left").with_columns(
        pl.when(pl.col("_plain_exists").fill_null(False) & (pl.col("_tbase") != ""))
        .then(pl.col("_tbase"))
        .otherwise(pl.col("_tkey"))
        .alias("_tgroup")
    ).drop("_plain_exists")


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
    groups: dict[str, list[_Row]] = {}
    for row in members:
        groups.setdefault(row.group, []).append(row)
    for group in groups.values():
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
