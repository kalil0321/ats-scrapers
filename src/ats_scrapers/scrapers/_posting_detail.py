"""Read the job body from an ordinary HTTP posting, without a browser."""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import TYPE_CHECKING, Any

from ats_scrapers.scrapers._htmltree import parse_html

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ats_scrapers.models import Job

_LD_SCRIPT = re.compile(
    r"""<script\b[^>]*\stype\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script\s*>""",
    re.IGNORECASE | re.DOTALL,
)


def job_posting(page: str) -> dict[str, Any]:
    for block in _LD_SCRIPT.finditer(page):
        try:
            data = json.loads(block[1])
        except ValueError:
            # Some posting templates also entity-encode the JSON quotes.
            try:
                data = json.loads(html.unescape(block[1]))
            except ValueError:
                continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return entry
            graph = entry.get("@graph")
            if isinstance(graph, list):
                entries.extend(graph)
    return {}


_ENCODED_TAG = re.compile(r"&lt;\s*/?\s*[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*?)?\s*/?\s*&gt;")


def decode_encoded_tags(value: str) -> str:
    """Decode markup sent entity-encoded (``&lt;p&gt;``) so it is cleaned as
    HTML. Real markup, whose encoded tags are literal text, and plain text
    with a lone ``&lt;`` are returned as they are."""
    if "<" in value or not _ENCODED_TAG.search(value):
        return value
    return html.unescape(value)


def body_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    value = decode_encoded_tags(value)
    if "<" in value:
        return parse_html(value).lines()
    return html.unescape(value).strip()


async def fill_missing(
    jobs: list[Job], enrich: Callable[[Job], Awaitable[None]], concurrency: int,
) -> None:
    """Use a fixed pool even when a board has tens of thousands of postings."""
    pending = (job for job in jobs if not job.description)

    async def worker() -> None:
        for job in pending:
            await enrich(job)

    workers = [asyncio.ensure_future(worker()) for _ in range(min(concurrency, len(jobs)))]
    try:
        await asyncio.gather(*workers)
    finally:
        # One failure ends the pass: no worker may go on reading through a
        # client its caller is about to close.
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
