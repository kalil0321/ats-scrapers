from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers.taleo_enterprise import TaleoEnterpriseScraper, _parse_slug
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency, _tolerated_shard_failures

MIN_ROWS = 138
_OPAQUE_ZONE = re.compile(r"^(?:[a-z]{2}\d{3}|tas-[a-z0-9-]+)$")


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/taleo_enterprise.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_taleo_enterprise_pipeline_config() -> None:
    config = CONFIGS["taleo_enterprise"]
    row = {
        "name": "Valero",
        "slug": "valero.taleo.net/2",
        "url": "https://valero.taleo.net/careersection/2/jobsearch.ftl",
    }

    assert config["scraper"] is TaleoEnterpriseScraper
    assert config["slug"](row) == "valero.taleo.net/2"
    assert config["kwargs"](row) == {"company_name": "Valero"}
    assert config["csv"] == "ats-companies/taleo_enterprise.csv"
    assert config["output"] == "taleo_enterprise/jobs.csv"
    # A requisition listed in several sections of a zone is published once.
    assert config["dedupe_by_ats_id"] is True
    # The listing carries title, location and date; only the description
    # needs the detail page, so it is read once and cached.
    assert config["defer_descriptions_to_cache"] is True
    assert _bounded_concurrency(config, 24) == 4


def test_taleo_enterprise_slug_falls_back_to_the_url_column() -> None:
    config = CONFIGS["taleo_enterprise"]
    row = {
        "name": "Valero",
        "slug": "",
        "url": "https://valero.taleo.net/careersection/2/jobsearch.ftl",
    }

    slug = config["slug"](row)

    assert _parse_slug(slug) == ("valero.taleo.net", "2")


def test_taleo_enterprise_fails_closed_on_errors_missing_sections_and_empty_output() -> None:
    config = CONFIGS["taleo_enterprise"]

    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True


def test_taleo_enterprise_tolerance_covers_one_section_in_twenty() -> None:
    config = CONFIGS["taleo_enterprise"]
    targets = len(_rows())
    tolerated = _tolerated_shard_failures(config, targets)

    assert config["fail_closed_tolerance"] == 0.05
    # One section being down does not hold back every other employer...
    assert tolerated >= 1
    # ...but the guard still trips well before a real outage.
    assert tolerated <= targets // 20


def test_taleo_enterprise_is_a_direct_employer_source_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["taleo_enterprise"] == ATS_DEDUP_PRIORITY["workday"] == 1


def test_taleo_enterprise_catalogue_is_one_row_per_career_section() -> None:
    rows = _rows()

    assert len(rows) >= MIN_ROWS
    assert len({row["slug"].casefold() for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)
    for row in rows:
        host, section = _parse_slug(row["slug"])
        assert row["slug"] == f"{host}/{section}"
        assert row["url"] == f"https://{host}/careersection/{section}/jobsearch.ftl"
        # An opaque zone code (``aa308``, ``tas-acme``) is not an employer
        # name, and ``s01...`` zones are staging copies.
        assert not _OPAQUE_ZONE.match(row["name"].casefold())
        assert not host.startswith("s01")
