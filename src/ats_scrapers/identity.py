"""Posting identity: the ``global_id`` of a job and the board it belongs to."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

# Providers whose job ids repeat across employers: an id is unique only on one
# board, so ``global_id`` carries that board.
TENANT_SCOPED_ATS = frozenset({
    "avature", "bamboohr", "beisen", "cornerstone", "eightfold", "icims",
    "jobvite", "personio", "phenom", "successfactors", "taleo",
})


def posting_tenant(ats_type: str, url: str) -> str | None:
    """The board a tenant-scoped provider's job ids are unique within, read
    from the posting URL: its host, plus the ``org`` parameter on Taleo
    Business Edition and the company path on Jobvite, whose hosts are shared.
    """
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").removeprefix("www.")
    except ValueError:
        return None
    if not host:
        return None
    if ats_type == "taleo":
        org = parse_qs(parts.query).get("org", [""])[0].strip()
        return f"{host}/{org.upper()}" if org else host
    if ats_type == "jobvite":
        segments = parts.path.strip("/").lower().split("/")
        if segments[0] == "careers":
            # ``/careers/{company}/job/{id}`` and ``/{company}/job/{id}`` are one board.
            segments = segments[1:]
        return f"{host}/{segments[0]}" if segments and segments[0] else host
    return host


def compose_global_id(ats_type: str, ats_id: str, url: str | None) -> str:
    """``{ats_type}:{ats_id}``, or ``{ats_type}:{board}:{ats_id}`` for a
    provider in ``TENANT_SCOPED_ATS``. Split on the first colon only: the
    rest is opaque."""
    tenant = posting_tenant(ats_type, url) if url and ats_type in TENANT_SCOPED_ATS else None
    return f"{ats_type}:{tenant}:{ats_id}" if tenant else f"{ats_type}:{ats_id}"


__all__ = ["TENANT_SCOPED_ATS", "compose_global_id", "posting_tenant"]
