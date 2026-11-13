from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers.talentsoft import TalentsoftScraper, _parse_slug
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency, _tolerated_shard_failures

MIN_ROWS = 161

# Labels of the copies customers keep beside their live site (test,
# pre-production, training): they list invented or copied offers.
COPY_LABEL_RE = re.compile(
    r"^(test|recette|preprod|demo|formation|sandbox|uat)|"
    r"-(test|recette|preprod|demo|formation|sandbox|uat)\d*$"
)


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/talentsoft.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_talentsoft_pipeline_config() -> None:
    config = CONFIGS["talentsoft"]
    row = {
        "name": "Dassault Aviation",
        "slug": "dassault-aviation-cand",
        "url": "https://dassault-aviation-cand.talent-soft.com",
    }

    assert config["scraper"] is TalentsoftScraper
    assert config["slug"](row) == "dassault-aviation-cand"
    assert config["kwargs"](row) == {"company_name": "Dassault Aviation"}
    assert config["csv"] == "ats-companies/talentsoft.csv"
    assert config["output"] == "talentsoft/jobs.csv"
    assert _bounded_concurrency(config, 24) == 6


def test_talentsoft_slug_falls_back_to_the_url_column() -> None:
    config = CONFIGS["talentsoft"]
    row = {"name": "Acme", "slug": "", "url": "https://acme-recrute.talent-soft.com"}

    assert _parse_slug(config["slug"](row)) == "acme-recrute"


def test_talentsoft_fails_closed_with_a_small_tolerance() -> None:
    config = CONFIGS["talentsoft"]
    targets = len(_rows())
    tolerated = _tolerated_shard_failures(config, targets)

    assert config["fail_closed_on_any_error"] is True
    # Sites that are gone count against the tolerance too.
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_tolerance"] == 0.05
    # A few sites being down or gone does not hold back the others...
    assert tolerated >= 1
    # ...but the guard still trips well before a real outage.
    assert tolerated <= targets // 20


def test_talentsoft_is_a_direct_employer_source_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["talentsoft"] == ATS_DEDUP_PRIORITY["workday"] == 1


def test_talentsoft_catalogue_is_one_row_per_career_site() -> None:
    rows = _rows()

    assert len(rows) >= MIN_ROWS
    assert len({row["slug"] for row in rows}) == len(rows)
    names = [row["name"].casefold() for row in rows]
    assert names == sorted(names)
    for row in rows:
        assert row["name"].strip() == row["name"] != ""
        # The employer, not the site label.
        assert row["name"].casefold() != row["slug"]
        assert _parse_slug(row["slug"]) == row["slug"]
        assert row["url"] == f"https://{row['slug']}.talent-soft.com"
        assert not COPY_LABEL_RE.search(row["slug"]), row["slug"]
