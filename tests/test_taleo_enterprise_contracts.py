from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers.taleo_enterprise import _parse_slug

MIN_ROWS = 138
# Zones whose sections list different employers. Their sections were
# compared and share no requisition (see docs/DISCOVERY_RULES.md).
MULTI_EMPLOYER_ZONES = {"edmonton.taleo.net"}
_OPAQUE_ZONE = re.compile(r"^(?:[a-z]{2}\d{3}|tas-[a-z0-9-]+)$")


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/taleo_enterprise.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


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


def test_taleo_enterprise_sections_of_a_zone_share_one_employer_name() -> None:
    """A requisition listed in two sections of a zone is published once,
    under the name of whichever section is read first: two names for one
    zone would make the published employer depend on the order of the run."""
    names: dict[str, set[str]] = {}
    for row in _rows():
        host, _ = _parse_slug(row["slug"])
        names.setdefault(host, set()).add(row["name"])

    several = {host for host, found in names.items() if len(found) > 1}

    assert several == MULTI_EMPLOYER_ZONES
