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

from jobhive.scrapers.eures import EuresScraper


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
# Streaming mode — :meth:`_fetch_async(on_job=...)` and :meth:`fetch_stream`
# ---------------------------------------------------------------------------
#
# Memory model: projection from a 10 k FR sample showed the legacy
# list-accumulating ``_fetch_async`` would peak at ~10 GB RSS on the
# ~2.7 M-job full corpus — over the 7.6 GB VPS RAM. The streaming
# variant pushes each parsed Job to an async callback (or asyncio.Queue
# in the ``fetch_stream`` wrapper) instead of accumulating, leaving
# only the ``seen`` ID set in memory (~100 MB at full scale).


def _fake_exhaust(items_to_emit):
    """Build a coroutine that mimics ``_exhaust_query``'s contract:
    it calls the supplied ``absorb`` callback with a single batch of
    items, then returns."""
    async def _fake(client, sem, *, base, depth, used_dims, absorb):
        await absorb(items_to_emit)
    return _fake


def test_on_job_callback_invoked_per_deduped_job(monkeypatch) -> None:
    """``_fetch_async(on_job=cb)`` must call ``cb`` for every parsed
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

    out = asyncio.run(scraper._fetch_async(on_job=collect))
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

    out = asyncio.run(scraper._fetch_async())
    assert [j.ats_id for j in out] == ["a", "b"]


def test_fetch_stream_yields_same_jobs_as_legacy(monkeypatch) -> None:
    """``fetch_stream()`` is an async-iterator façade over
    ``_fetch_async`` — every job legacy ``_fetch_async`` would have
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


def test_fetch_stream_terminates_cleanly_when_all_countries_fail(
    monkeypatch, caplog
) -> None:
    """``_gather_tolerant`` (used for the country fan-out) swallows
    per-country exceptions by design — see PR #35. ``fetch_stream``
    must therefore still terminate cleanly when every country task
    raises: the consumer iterator finishes with zero jobs and the
    failures show up as warning logs (not a re-raised error)."""
    scraper = EuresScraper("eures")

    async def boom(client, sem, *, base, depth, used_dims, absorb):
        raise RuntimeError("network down")

    monkeypatch.setattr(scraper, "_exhaust_query", boom)

    import logging
    async def consume():
        out = []
        async for job in scraper.fetch_stream():
            out.append(job)
        return out

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(consume())
    assert result == []
    # The per-country failures are logged so an operator can see them.
    assert any(
        "EURES country subtask failed" in r.getMessage()
        for r in caplog.records
    )
