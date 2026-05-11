"""Unit tests for the EURES partial-failure-tolerant gather helper.

The 2026-05-11 cron run dropped EURES from ~1 M to 12 k rows because
a deep-recursion sibling raised (network blip under contention) and
``asyncio.gather`` cancelled every other task. ``_gather_tolerant``
swaps in ``return_exceptions=True`` + a warning log so the rest of
the tree keeps writing.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from jobhive.scrapers.eures import _gather_tolerant


@pytest.mark.asyncio
async def test_gather_tolerant_runs_every_sibling_when_one_raises(
    caplog,
) -> None:
    """Sibling tasks must complete even if one raises — the default
    ``asyncio.gather`` would have cancelled them all."""
    completed: list[int] = []

    async def succeed(i: int) -> None:
        await asyncio.sleep(0)
        completed.append(i)

    async def fail() -> None:
        raise RuntimeError("transient network blip")

    coros = [succeed(1), fail(), succeed(2), succeed(3)]

    with caplog.at_level(logging.WARNING):
        await _gather_tolerant(coros, label="page")

    assert sorted(completed) == [1, 2, 3]
    # And the failure surfaced as a logged warning so the operator can
    # see how much was lost.
    assert any(
        "EURES page subtask failed" in r.getMessage()
        and "transient network blip" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_gather_tolerant_no_failures_is_silent(caplog) -> None:
    """No warning when every sibling succeeds — the helper must not
    add noise to the happy path."""
    async def succeed() -> None:
        return None

    with caplog.at_level(logging.WARNING):
        await _gather_tolerant([succeed() for _ in range(5)], label="country")

    assert not any(
        "subtask failed" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_gather_tolerant_label_appears_in_warning(caplog) -> None:
    """The ``label`` argument distinguishes which recursion level the
    failure happened at (country / region / sector / schedule / page)."""
    async def fail() -> None:
        raise ValueError("boom")

    with caplog.at_level(logging.WARNING):
        await _gather_tolerant([fail()], label="sector")

    assert any(
        "EURES sector subtask failed" in r.getMessage()
        for r in caplog.records
    )
