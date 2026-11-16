from __future__ import annotations

import csv
import re
from pathlib import Path

EXPECTED_ROWS = 348


def test_greeting_catalogue_is_well_formed() -> None:
    with Path("ats-companies/greeting.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == EXPECTED_ROWS
    slugs = [row["slug"] for row in rows]
    assert len(set(slugs)) == len(slugs)
    assert slugs == sorted(slugs, key=lambda slug: (slug.casefold(), slug))
    for row in rows:
        assert row["name"].strip()
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", row["slug"])
        assert row["url"] == f"https://{row['slug']}.career.greetinghr.com"
