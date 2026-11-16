from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers.talentsoft import _parse_slug

MIN_ROWS = 164

# Labels of the copies customers keep beside their live site (test,
# pre-production, training): they list invented or copied offers.
COPY_LABEL_RE = re.compile(
    r"^(test|recette|preprod|demo|formation|sandbox|uat)|"
    r"-(test|recette|preprod|demo|formation|sandbox|uat)\d*$"
)


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/talentsoft.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


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
