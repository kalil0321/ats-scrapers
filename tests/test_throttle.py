"""Tests for request pacing."""

from __future__ import annotations

import asyncio
import time

from ats_scrapers.scrapers._throttle import Throttle


async def test_throttle_spaces_request_starts() -> None:
    throttle = Throttle(4, 0.05)
    starts: list[float] = []

    async def request() -> None:
        async with throttle:
            starts.append(time.monotonic())

    await asyncio.gather(*(request() for _ in range(3)))

    assert starts[2] - starts[0] >= 0.09


async def test_throttle_limits_concurrency() -> None:
    throttle = Throttle(2, 0)
    running = peak = 0

    async def request() -> None:
        nonlocal running, peak
        async with throttle:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(request() for _ in range(6)))

    assert peak == 2
