from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers.peopleadmin import _parse_slug

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
