"""Every source maps to exactly one kind, and only employer sources
count toward the headline total."""

from __future__ import annotations

import pytest

from ats_scrapers.models import ATSType
from ats_scrapers.source_kind import (
    COMPANY_SITES,
    GOV_BOARDS,
    JOB_BOARDS,
    is_headline,
    source_kind,
)


def test_explicit_lists_do_not_overlap() -> None:
    assert not COMPANY_SITES & GOV_BOARDS
    assert not COMPANY_SITES & JOB_BOARDS
    assert not GOV_BOARDS & JOB_BOARDS


def test_explicit_lists_name_known_sources() -> None:
    known = {a.value for a in ATSType}
    assert known >= (COMPANY_SITES | GOV_BOARDS | JOB_BOARDS)


@pytest.mark.parametrize(
    ("source", "kind", "headline"),
    [
        ("workday", "ats", True),
        ("greenhouse", "ats", True),
        ("google", "company_site", True),
        ("eures", "gov_board", False),
        ("bundesagentur", "gov_board", False),
        ("welcometothejungle", "job_board", False),
        ("some_new_ats", "ats", True),
        (" EURES ", "gov_board", False),
    ],
)
def test_source_kind(source: str, kind: str, headline: bool) -> None:
    assert source_kind(source) == kind
    assert is_headline(source) is headline
