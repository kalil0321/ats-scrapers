"""Command-line entry points.

Four commands are installed with the package:

``ats-scraper-search``
    Filter the hosted dataset (same filters as :func:`ats_scrapers.search`).
``ats-scraper-find``
    Find which ATS a company is on (:func:`ats_scrapers.find_company`).
``ats-scraper-fetch``
    Scrape one company live, given its ATS and slug.
``ats-scraper-fetch-url``
    Scrape one company live from a public careers URL.

Every command prints JSON on stdout, so the output pipes into ``jq``.
The two ``fetch`` commands need the scraper extra:
``pip install 'ats-scrapers[scrapers]'``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    import pandas as pd

    from ats_scrapers.models import Job

_SCRAPERS_EXTRA_HINT = "this command needs the scraper extra: pip install 'ats-scrapers[scrapers]'"


def _fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _print_frame(df: pd.DataFrame) -> None:
    # Round-trip through pandas' encoder (NaN -> null, timestamps -> ISO),
    # then re-dump so URLs keep their plain slashes ("https://", not "https:\/\/").
    records = json.loads(df.to_json(orient="records", date_format="iso"))
    print(json.dumps(records, indent=2, ensure_ascii=False))


def _print_jobs(jobs: list[Job]) -> None:
    from pydantic import TypeAdapter

    from ats_scrapers.models import Job

    print(TypeAdapter(list[Job]).dump_json(jobs, indent=2).decode())


def search(argv: Sequence[str] | None = None) -> None:
    """``ats-scraper-search``: filter the hosted dataset."""
    parser = argparse.ArgumentParser(
        prog="ats-scraper-search",
        description="Search the hosted job dataset. Prints matching jobs as JSON.",
    )
    parser.add_argument("--query", help="substring of the job title")
    parser.add_argument("--location", help="substring of the job location")
    parser.add_argument("--company", help="substring of the company name")
    # No enum choices: the hosted dataset can publish a source before the
    # installed package knows it, and the dataset client checks the name.
    parser.add_argument(
        "--ats-type", dest="ats", metavar="ATS", help="source, e.g. greenhouse or jobs_cz"
    )
    # Tri-state: omitted means "no filter", not "non-remote only".
    parser.add_argument(
        "--remote",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="keep only remote jobs (--no-remote: only non-remote jobs)",
    )
    parser.add_argument("--salary-min", type=float)
    parser.add_argument("--salary-max", type=float)
    parser.add_argument("--experience-max", type=int, help="maximum years of experience")
    parser.add_argument("--limit", type=int, help="maximum number of jobs to print")
    args = parser.parse_args(argv)

    from ats_scrapers.client import search as search_dataset
    from ats_scrapers.exceptions import ATSScrapersError

    try:
        jobs = search_dataset(
            args.query,
            location=args.location,
            company=args.company,
            ats=args.ats,
            remote=args.remote,
            salary_min=args.salary_min,
            salary_max=args.salary_max,
            experience_max=args.experience_max,
            limit=args.limit,
        )
    except ATSScrapersError as exc:
        _fail(str(exc))
    _print_frame(jobs)


def find(argv: Sequence[str] | None = None) -> None:
    """``ats-scraper-find``: which ATS is a company on?"""
    parser = argparse.ArgumentParser(
        prog="ats-scraper-find",
        description="Find tracked companies by (partial) name or slug. Prints matches as JSON.",
    )
    parser.add_argument("company", help="company name or slug")
    parser.add_argument(
        "--limit", type=int, default=10, help="maximum number of matches (default: 10)"
    )
    args = parser.parse_args(argv)

    from ats_scrapers.client import find_company

    _print_frame(find_company(args.company, limit=args.limit))


def fetch(argv: Sequence[str] | None = None) -> None:
    """``ats-scraper-fetch``: scrape one company live."""
    parser = argparse.ArgumentParser(
        prog="ats-scraper-fetch",
        description="Scrape one company's open jobs live. Prints the jobs as JSON.",
    )
    parser.add_argument("ats", metavar="ATS", help="ATS of the company, e.g. greenhouse")
    parser.add_argument("company", help="company slug on that ATS, e.g. anthropic")
    parser.add_argument(
        "--no-descriptions",
        dest="include_descriptions",
        action="store_false",
        help="skip job descriptions (faster on boards that need one request per job)",
    )
    args = parser.parse_args(argv)

    try:
        from ats_scrapers.scrapers import ScraperRegistry
    except ImportError:
        _fail(_SCRAPERS_EXTRA_HINT)

    if not ScraperRegistry.has_scraper(args.ats):
        available = ", ".join(sorted(source.value for source in ScraperRegistry.all()))
        parser.error(f"no scraper for {args.ats!r}. Available: {available}")

    from ats_scrapers.exceptions import ATSScrapersError

    try:
        scraper = ScraperRegistry.get(args.ats)(
            args.company,
            include_descriptions=args.include_descriptions,
        )
    except ATSScrapersError as exc:  # e.g. a careers URL where a slug is expected
        _fail(str(exc))
    _print_jobs(_run(scraper))


def fetch_for_url(argv: Sequence[str] | None = None) -> None:
    """``ats-scraper-fetch-url``: scrape one company live from its careers URL."""
    parser = argparse.ArgumentParser(
        prog="ats-scraper-fetch-url",
        description="Scrape the jobs behind a public careers URL. Prints the jobs as JSON.",
    )
    parser.add_argument("url", help="careers page URL, e.g. https://boards.greenhouse.io/anthropic")
    parser.add_argument(
        "--no-descriptions",
        dest="include_descriptions",
        action="store_false",
        help="skip job descriptions",
    )
    args = parser.parse_args(argv)

    try:
        from ats_scrapers.resolve import get_scraper_for_url
    except ImportError:
        _fail(_SCRAPERS_EXTRA_HINT)

    from ats_scrapers.exceptions import ScraperError

    try:
        scraper = get_scraper_for_url(args.url, include_descriptions=args.include_descriptions)
    except ScraperError as exc:
        _fail(str(exc))
    _print_jobs(_run(scraper))


def _run(scraper: object) -> list[Job]:
    from ats_scrapers.exceptions import ATSScrapersError

    try:
        return scraper.fetch()  # type: ignore[attr-defined,no-any-return]
    except ImportError:
        _fail(_SCRAPERS_EXTRA_HINT)
    except ATSScrapersError as exc:
        _fail(str(exc))
