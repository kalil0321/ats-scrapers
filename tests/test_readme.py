from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).parents[1] / "README.md"
_NUMBER = r"\d[\d,.]*(?:\s*(?:[KMB]|thousand|million|billion))?\+?"
_METRIC = r"(?:jobs?|job\s+(?:postings?|listings?)|companies|tenants|sources|adapters?)"
_HARD_CODED_DATASET_STAT = re.compile(
    rf"(?:\b{_NUMBER}(?:\s+(?:live|active|open|hosted|public|reusable|scraper))*"
    rf"\s+{_METRIC}\b|\b{_METRIC}\s*(?:count|total)?\s*[:=]\s*{_NUMBER}\b)",
    flags=re.IGNORECASE,
)


def _readme_prose(content: str) -> str:
    lines: list[str] = []
    in_code_fence = False
    for line in content.splitlines():
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if not in_code_fence and "img.shields.io/badge/dynamic/json" not in line:
            lines.append(line)
    return "\n".join(lines)


def test_readme_uses_live_manifest_for_mutable_dataset_stats() -> None:
    content = README.read_text()

    for query in (
        "%24.stats.total_jobs",
        "%24.stats.total_companies",
        "%24.stats.ats_count",
        "%24.generated_at",
    ):
        assert query in content

    assert _HARD_CODED_DATASET_STAT.search(_readme_prose(content)) is None


def test_hard_coded_dataset_stat_guard_is_wording_independent() -> None:
    claims = (
        "**4.2M+ live jobs**",
        "**1,200+ active job postings**",
        "4.2 million jobs",
        "79,906 companies",
        "(63,000+ tenants)",
        "**49 sources**",
        "More than 50 reusable scraper adapters",
        "jobs total: 4.2M",
    )

    for claim in claims:
        assert _HARD_CODED_DATASET_STAT.search(claim) is not None
