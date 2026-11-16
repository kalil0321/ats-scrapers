"""Request pacing for sites that block an address asking for pages too fast."""

from __future__ import annotations

import asyncio


class Throttle:
    """At most ``concurrency`` requests at a time, started ``interval`` seconds apart.

    Use as ``async with throttle:`` around one request.
    """

    def __init__(self, concurrency: int, interval: float) -> None:
        self._slots = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._interval = interval
        self._next_start = 0.0

    async def __aenter__(self) -> None:
        await self._slots.acquire()
        try:
            async with self._lock:
                loop = asyncio.get_running_loop()
                delay = self._next_start - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_start = loop.time() + self._interval
        except BaseException:
            self._slots.release()
            raise

    async def __aexit__(self, *exc_info: object) -> None:
        self._slots.release()
