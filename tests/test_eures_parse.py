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

import asyncio

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.scrapers.eures import (
    EuresScraper,
    _clean_description_text,
    _extract_detail_description,
)


def _base_item(**overrides):
    """Minimal valid EURES API payload row, overridable per test."""
    base = {
        "id": "abc123",
        "title": "Software Engineer",
        "description": "<p>Build European services.</p>",
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
    assert job.description == "Build European services."


def test_metadata_alone_is_not_a_job_description() -> None:
    payload = {"jvProfiles": {"bg": {
        "description": "<p>&nbsp;</p>",
        "employer": {"description": "Company profile"},
        "requiredSkills": [{"prefLabel": "Driving"}],
        "applicationInstructions": ["Call the employer."],
    }}}

    assert _extract_detail_description(payload) is None


def test_detail_prefers_original_profile_then_falls_back_across_languages() -> None:
    profiles = {
        "en": {"description": "Call the employer."},
        "de": {"description": "<p>Bitte anrufen.</p>"},
    }
    payload = {"preferredLanguage": "de", "jvProfiles": profiles,
               "translation": {"description": "Translated text"}}
    assert _extract_detail_description(payload) == "Bitte anrufen."
    profiles["de"]["description"] = "<br>"
    assert _extract_detail_description(payload) == "Call the employer."
    profiles.clear()
    assert _extract_detail_description(payload) == "Translated text"


def test_zero_width_description_is_ignored() -> None:
    job = EuresScraper("eures")._parse(_base_item(description="\u200b"))

    assert job is not None
    assert job.description is None


def test_schedule_list_sets_employment_type_without_contract_type() -> None:
    job = EuresScraper("eures")._parse(
        _base_item(positionOfferingCode="NS",
                   positionScheduleCodes=["flextime", "parttime"])
    )

    assert job is not None
    assert job.employment_type == "PART_TIME"
    assert job.raw["positionScheduleCodes"] == ["flextime", "parttime"]


def test_contract_type_wins_over_schedule() -> None:
    job = EuresScraper("eures")._parse(
        _base_item(positionOfferingCode="temporary",
                   positionScheduleCodes=["fulltime"])
    )

    assert job is not None
    assert job.employment_type == "TEMPORARY"


@pytest.mark.parametrize(("response", "expected"), [
    ({"jvProfiles": {"en": {"description": "Apply through EURES."}}}, "Apply through EURES."),
    ({"jvProfiles": {"en": {"applicationInstructions": ["Apply through EURES."]}}}, None),
    (["unexpected"], None),
])
def test_get_description_uses_only_detail_job_body(httpx_mock, response, expected) -> None:
    httpx_mock.add_response(
        url="https://europa.eu/eures/api/jv-searchengine/public/jv/id/abc123?requestLang=en&preferredLang=en",
        json=response,
    )
    scraper = EuresScraper("eures")
    job = scraper._parse(_base_item(description="", locationMap={"DE": ["DE1"]}))
    assert scraper.get_description(job) == expected


def test_get_description_returns_none_for_vanished_job(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://europa.eu/eures/api/jv-searchengine/public/jv/id/gone?requestLang=en&preferredLang=en",
        status_code=404,
    )
    scraper = EuresScraper("eures")
    job = scraper._parse(_base_item(id="gone", description="", locationMap={"DE": ["DE1"]}))
    assert scraper.get_description(job) is None
    assert scraper.get_description(job.model_copy(update={"ats_id": None})) is None


def test_short_listing_description_needs_no_detail_request(httpx_mock) -> None:
    scraper = EuresScraper("eures")
    job = scraper._parse(_base_item(description="OK"))
    assert job.description == scraper.get_description(job) == "OK"
    assert not httpx_mock.get_requests()


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


# ---------------------------------------------------------------------------
# Streaming mode — :meth:`afetch(on_job=...)` and :meth:`fetch_stream`
# ---------------------------------------------------------------------------
#
# Memory model: projection from a 10 k FR sample showed the legacy
# list-accumulating ``afetch`` would peak at ~10 GB RSS on the
# ~2.7 M-job full corpus, more than an 8 GB server has. The streaming
# variant pushes each parsed Job to an async callback (or asyncio.Queue
# in the ``fetch_stream`` wrapper) instead of accumulating, leaving
# only the ``seen`` ID set in memory (~100 MB at full scale).


def _fake_exhaust(items_to_emit):
    """Build a coroutine that mimics ``_exhaust_query``'s contract:
    it calls the supplied ``absorb`` callback with a single batch of
    items, then returns."""
    async def _fake(fetcher, sem, *, base, absorb, slices=None):
        await absorb(items_to_emit)
    return _fake


def test_on_job_callback_invoked_per_deduped_job(monkeypatch) -> None:
    """``afetch(on_job=cb)`` must call ``cb`` for every parsed
    job that survives dedup, and must NOT accumulate them into the
    returned list. Dedup is by ``ats_id``."""
    scraper = EuresScraper("eures")
    items = [
        _base_item(id="a", title="Job A"),
        _base_item(id="b", title="Job B"),
        _base_item(id="a", title="Job A duplicate"),   # same id — dropped
        _base_item(id="c", title="Job C", employerName="non renseigné"),
        _base_item(id="d", title="", employerName="OK"),  # blank title — dropped
    ]
    monkeypatch.setattr(scraper, "_exhaust_query", _fake_exhaust(items))

    received: list = []
    async def collect(job):
        received.append(job)

    out = asyncio.run(scraper.afetch(on_job=collect))
    # Streaming mode → returned list is empty.
    assert out == []
    # 3 jobs survived dedup + blank-title filter (a, b, c).
    assert [j.ats_id for j in received] == ["a", "b", "c"]
    # Anonymous-employer row still survives (verbatim, not dropped).
    assert received[2].company == "non renseigné"


def test_on_job_none_accumulates_to_list(monkeypatch) -> None:
    """Without an ``on_job`` sink we keep the legacy behaviour:
    every deduped job lands in the returned list."""
    scraper = EuresScraper("eures")
    items = [
        _base_item(id="a"),
        _base_item(id="b"),
        _base_item(id="a"),  # dup
    ]
    monkeypatch.setattr(scraper, "_exhaust_query", _fake_exhaust(items))

    out = asyncio.run(scraper.afetch())
    assert [j.ats_id for j in out] == ["a", "b"]


def test_fetch_stream_yields_same_jobs_as_legacy(monkeypatch) -> None:
    """``fetch_stream()`` is an async-iterator façade over
    ``afetch`` — every job legacy ``afetch`` would have
    returned must come out of the stream in some order."""
    scraper = EuresScraper("eures")
    items = [_base_item(id=str(i)) for i in range(7)]
    monkeypatch.setattr(scraper, "_exhaust_query", _fake_exhaust(items))

    async def collect_stream() -> list:
        out = []
        async for job in scraper.fetch_stream():
            out.append(job)
        return out

    streamed = asyncio.run(collect_stream())
    assert sorted(j.ats_id for j in streamed) == sorted(str(i) for i in range(7))


def test_fetch_stream_raises_when_the_portal_query_fails(monkeypatch) -> None:
    """Failed sub-slices are logged by ``_gather_tolerant``; a failed
    top-level query means nothing was collected and must surface."""
    scraper = EuresScraper("eures")

    async def boom(fetcher, sem, *, base, absorb, slices=None):
        raise ScraperError("network down")

    monkeypatch.setattr(scraper, "_exhaust_query", boom)

    async def consume():
        return [job async for job in scraper.fetch_stream()]

    with pytest.raises(ScraperError, match="network down"):
        asyncio.run(consume())


def test_listing_uses_translation_if_original_contains_only_markup() -> None:
    item = _base_item(description="<br>", translations={
        "en": {"description": "&#8203;"},
        "de": {"description": "E-Mail: &lt;info@example.org&gt;"},
    })
    job = EuresScraper("eures")._parse(item)
    assert job.description == "E-Mail: <info@example.org>"


@pytest.mark.parametrize(("value", "expected"), [
    ("&lt;p&gt;Drive trucks.&lt;/p&gt;", "Drive trucks."),
    ("<p>Use the &lt;div&gt; element.</p>", "Use the <div> element."),
    ("Pay &lt; 3,000 EUR", "Pay < 3,000 EUR"),
])
def test_description_text_reads_encoded_markup_and_keeps_literal_brackets(
    value: str, expected: str,
) -> None:
    assert _clean_description_text(value) == expected
