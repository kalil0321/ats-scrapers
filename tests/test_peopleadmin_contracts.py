from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers.peopleadmin import PeopleAdminScraper, _parse_slug
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency, _tolerated_shard_failures

MIN_ROWS = 111

# Labels of the copies institutions keep beside their live portal (sandbox,
# training, test): they list invented or copied postings.
COPY_LABEL_RE = re.compile(
    r"-(training|train|sb|sandbox|test|dev|stg|stage|staging|uat|demo|impl|qa"
    r"|upgrade|preprod|pprd|trn|tst)\d*$"
)


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/peopleadmin.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_peopleadmin_pipeline_config() -> None:
    config = CONFIGS["peopleadmin"]
    row = {
        "name": "University of Utah",
        "slug": "utah",
        "url": "https://utah.peopleadmin.com",
    }

    assert config["scraper"] is PeopleAdminScraper
    assert config["slug"](row) == "utah"
    assert config["kwargs"](row) == {"company_name": "University of Utah"}
    assert config["csv"] == "ats-companies/peopleadmin.csv"
    assert config["output"] == "peopleadmin/jobs.csv"
    assert _bounded_concurrency(config, 24) == 6


def test_peopleadmin_slug_falls_back_to_the_url_column() -> None:
    config = CONFIGS["peopleadmin"]
    row = {"name": "Acme", "slug": "", "url": "https://acme.peopleadmin.com"}

    assert _parse_slug(config["slug"](row)) == "acme"


def test_peopleadmin_fails_closed_with_a_small_tolerance() -> None:
    config = CONFIGS["peopleadmin"]
    targets = len(_rows())
    tolerated = _tolerated_shard_failures(config, targets)

    assert config["fail_closed_on_any_error"] is True
    # Portals that are gone count against the tolerance too.
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_tolerance"] == 0.05
    # A few portals being down or gone does not hold back the others...
    assert tolerated >= 1
    # ...but the guard still trips well before a real outage.
    assert tolerated <= targets // 20


def test_peopleadmin_is_a_direct_employer_source_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["peopleadmin"] == ATS_DEDUP_PRIORITY["workday"] == 1


def test_peopleadmin_catalogue_is_one_row_per_portal() -> None:
    rows = _rows()

    assert len(rows) >= MIN_ROWS
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["name"].casefold() for row in rows}) == len(rows)
    names = [row["name"].casefold() for row in rows]
    assert names == sorted(names)
    for row in rows:
        assert row["name"].strip() == row["name"] != ""
        # The institution, not the portal label.
        assert row["name"].casefold() != row["slug"]
        assert _parse_slug(row["slug"]) == row["slug"]
        assert row["url"] == f"https://{row['slug']}.peopleadmin.com"
        assert not COPY_LABEL_RE.search(row["slug"]), row["slug"]
