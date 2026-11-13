"""beautifulsoup4 is an optional extra (``ats-scrapers[scrapers]``).

Scraper modules are all imported by ``ats_scrapers.scrapers``, so none of
them may import ``bs4`` at module level: the package would then fail to
import without the extra. They parse through :func:`bs4_soup` instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ats_scrapers.exceptions import ScraperError

if TYPE_CHECKING:
    from bs4 import BeautifulSoup


def bs4_soup(html: str, *, scraper: str) -> BeautifulSoup:
    """``BeautifulSoup(html, "html.parser")``, or a ``ScraperError`` naming
    the missing extra when beautifulsoup4 is not installed."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScraperError(
            f"{scraper} scraper requires beautifulsoup4; "
            "install `ats-scrapers[scrapers]`"
        ) from exc
    return BeautifulSoup(html, "html.parser")
