"""Importing the scraper registry must not require optional extras."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

_BLOCK_BS4 = textwrap.dedent(
    """
    import sys

    sys.modules["bs4"] = None  # any `import bs4` now raises ImportError
    """
)


def _run_without_bs4(body: str) -> subprocess.CompletedProcess[str]:
    # A fresh interpreter: the parent has already imported every scraper module.
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_BS4 + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_scrapers_import_without_beautifulsoup4() -> None:
    result = _run_without_bs4(
        """
        from ats_scrapers.scrapers import get_scraper  # noqa: F401
        from ats_scrapers.scrapers.herp import HerpScraper  # noqa: F401
        from ats_scrapers.scrapers.hrmos import HrmosScraper  # noqa: F401
        from ats_scrapers.scrapers.keka import KekaScraper  # noqa: F401
        """
    )

    assert result.returncode == 0, result.stderr


def test_bs4_scrapers_raise_scraper_error_when_parsing_without_beautifulsoup4() -> None:
    result = _run_without_bs4(
        """
        from ats_scrapers.exceptions import ScraperError
        from ats_scrapers.scrapers.herp import HerpScraper
        from ats_scrapers.scrapers.hrmos import HrmosScraper
        from ats_scrapers.scrapers.keka import _html_to_text

        for parse in (
            lambda: HerpScraper("acme")._parse_listing("<html></html>"),
            lambda: HrmosScraper("acme")._parse_page("<html></html>"),
            lambda: _html_to_text("<p>x</p>"),
        ):
            try:
                parse()
            except ScraperError as exc:
                assert "beautifulsoup4" in str(exc)
                assert "ats-scrapers[scrapers]" in str(exc)
            else:
                raise AssertionError("expected ScraperError")
        """
    )

    assert result.returncode == 0, result.stderr


def test_scrapers_import_without_any_optional_extra() -> None:
    """Every module the ``scrapers`` and ``parquet`` extras install is
    blocked: the registry still imports, with the same scrapers registered
    as in this interpreter, where the extras are installed."""
    from ats_scrapers.scrapers import ScraperRegistry

    blocked = ("bs4", "aiohttp", "html2text", "httpcloak", "cloakbrowser", "Crypto", "pyarrow")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            + "".join(f"sys.modules[{name!r}] = None\n" for name in blocked)
            + textwrap.dedent(
                """
                import json

                from ats_scrapers.scrapers import ScraperRegistry

                print(json.dumps(sorted(t.value for t in ScraperRegistry.all())))
                """
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == sorted(t.value for t in ScraperRegistry.all())


def test_shared_bs4_helper_raises_scraper_error_without_beautifulsoup4() -> None:
    """The scrapers that parse through ``_bs4.bs4_soup`` (ApplicantPro,
    AppliTrack, Hireology, Ninehire, Talentio, WinTalent) fail with the
    install hint, not with an ImportError."""
    result = _run_without_bs4(
        """
        from ats_scrapers.exceptions import ScraperError
        from ats_scrapers.scrapers._bs4 import bs4_soup
        from ats_scrapers.scrapers.hireology import _html_text

        for parse in (
            lambda: bs4_soup("<p>x</p>", scraper="Hireology"),
            lambda: _html_text("<p>x</p>"),
        ):
            try:
                parse()
            except ScraperError as exc:
                assert "Hireology scraper requires beautifulsoup4" in str(exc)
                assert "ats-scrapers[scrapers]" in str(exc)
            else:
                raise AssertionError("expected ScraperError")
        """
    )

    assert result.returncode == 0, result.stderr


def test_shared_bs4_helper_parses_when_beautifulsoup4_is_installed() -> None:
    from ats_scrapers.scrapers._bs4 import bs4_soup

    assert bs4_soup("<p>Hello <b>there</b></p>", scraper="Hireology").get_text() == "Hello there"
