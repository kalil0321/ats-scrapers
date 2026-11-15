"""Is a board's public page still there?

Some providers keep listing postings through their API after the employer's
public board is closed. Those postings cannot be opened by a reader, so the
scrapers that use this helper treat such a board as not found.
"""

from __future__ import annotations

import logging

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import Fetcher, FetchResponse

logger = logging.getLogger(__name__)

REDIRECTS = frozenset({301, 302, 303, 307, 308})
# The check is best effort: one attempt, so a slow or rate-limited page
# cannot hold up postings the API has already returned.
PAGE_CHECK_ATTEMPTS = 1


async def page_is_gone(fetch: Fetcher, url: str) -> bool:
    """True only when ``url`` answers 404. One HEAD request.

    Any other failure is inconclusive and counts as "still there": a page
    check must not cost an employer its jobs. Build ``fetch`` with
    ``retries=PAGE_CHECK_ATTEMPTS``.
    """
    response = await _head(fetch, url, handled={404})
    return response is not None and response.status_code == 404


async def redirect_target(fetch: Fetcher, url: str) -> str | None:
    """Where ``url`` redirects to, or ``None``. One HEAD request.

    ``fetch`` must have been built with ``follow_redirects=False``.
    """
    response = await _head(fetch, url, handled=REDIRECTS | {404})
    if response is None or response.status_code not in REDIRECTS:
        return None
    for name, value in response.headers.items():
        if name.lower() == "location":
            return value
    return None


async def _head(
    fetch: Fetcher, url: str, *, handled: frozenset[int] | set[int]
) -> FetchResponse | None:
    try:
        return await fetch.request("HEAD", url, handled=handled)
    except ScraperError as exc:
        logger.warning("Public page %s could not be checked: %s", url, exc)
        return None
