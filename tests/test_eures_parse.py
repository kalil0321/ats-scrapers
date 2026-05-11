"""Tests for EURES per-row parsing — specifically the
keep-anonymous-employer pass added in 2026-05.

Historical context: ~86% of EURES FR rows and ~60% of ES rows ship
with placeholder employer values ("non renseigné" for FR, empty
string for ES, plus a long tail of localized markers). Earlier
versions of ``_parse`` dropped these rows entirely; the user
requested in 2026-05 that we keep them — the underlying jobs are
real (titles, descriptions and locations are all meaningful) and
the locale of the placeholder is itself useful signal about the
source NES, so we pass the value through verbatim rather than
canonicalize it.
"""

from __future__ import annotations

from jobhive.scrapers.eures import EuresScraper


def _base_item(**overrides):
    """Minimal valid EURES API payload row, overridable per test."""
    base = {
        "id": "abc123",
        "title": "Software Engineer",
        "employerName": "Acme Corp",
        "locationMap": {},
        "creationDate": 1715000000000,
    }
    base.update(overrides)
    return base


def test_real_employer_passes_through_verbatim() -> None:
    item = _base_item(employerName="Acme Corp")
    job = EuresScraper("eures")._parse(item)
    assert job is not None
    assert job.company == "Acme Corp"


def test_french_non_renseigne_kept_verbatim() -> None:
    """86% of EURES FR rows. Must NOT be dropped, and must NOT be
    canonicalized — the locale string itself is information about
    the source NES (France Travail in this case)."""
    item = _base_item(employerName="non renseigné")
    job = EuresScraper("eures")._parse(item)
    assert job is not None
    assert job.company == "non renseigné"
    assert job.title == "Software Engineer"  # other fields still parsed


def test_empty_employer_kept_as_empty_string() -> None:
    """60% of ES rows ship ``employerName=""``. The row survives and
    ``Job.company`` is an empty string — downstream consumers can
    treat it as anonymous however they prefer."""
    item = _base_item(employerName="")
    job = EuresScraper("eures")._parse(item)
    assert job is not None
    assert job.company == ""


def test_missing_employer_field_yields_empty_string() -> None:
    item = _base_item()
    del item["employerName"]
    job = EuresScraper("eures")._parse(item)
    assert job is not None
    assert job.company == ""


def test_nested_employer_dict_supported() -> None:
    item = _base_item(employerName=None, employer={"name": "Real Co"})
    job = EuresScraper("eures")._parse(item)
    assert job is not None
    assert job.company == "Real Co"


def test_nested_employer_dict_with_placeholder_kept_verbatim() -> None:
    """Defensive: when ``employerName`` is missing/null and the
    nested ``employer.name`` is itself a placeholder, the row still
    survives and the placeholder text flows through. Guards against
    a future refactor accidentally short-circuiting the nested
    branch before the row-keep contract applies. (Flagged by
    Greptile on PR #68.)"""
    item = _base_item(employerName=None, employer={"name": "non renseigné"})
    job = EuresScraper("eures")._parse(item)
    assert job is not None
    assert job.company == "non renseigné"


def test_locale_specific_placeholders_kept_verbatim() -> None:
    """Spanish "no se especifica", German "siehe beschreibung",
    English "anonymous", etc. — all pass through with their
    original casing and language so downstream consumers can
    distinguish the source NES from the placeholder text."""
    for placeholder in (
        "non renseigné",
        "no se especifica",
        "siehe beschreibung",
        "anonymous",
        "Confidentiel",
        "konfidentiell",
    ):
        item = _base_item(employerName=placeholder)
        job = EuresScraper("eures")._parse(item)
        assert job is not None, f"placeholder {placeholder!r} dropped"
        assert job.company == placeholder, (
            f"placeholder {placeholder!r} got rewritten to {job.company!r}"
        )


def test_employer_whitespace_is_stripped() -> None:
    """Leading/trailing whitespace from the API value is trimmed —
    this normalization is fine because it doesn't change the
    semantic content, just removes a noisy artifact."""
    item = _base_item(employerName="  Acme Corp  ")
    job = EuresScraper("eures")._parse(item)
    assert job is not None
    assert job.company == "Acme Corp"


def test_missing_title_still_drops_row() -> None:
    """The row-drop behaviour for missing-essentials (title or id) is
    unchanged — only the employer-placeholder branch was relaxed."""
    item = _base_item(title="")
    assert EuresScraper("eures")._parse(item) is None
    item2 = _base_item()
    del item2["id"]
    assert EuresScraper("eures")._parse(item2) is None
