"""Kula career sites (``careers.kula.ai``).

Kula's documented jobs API needs the employer's own token, so this reads the
public career site, which every company has at one address:

    GET https://careers.kula.ai/{company}

The page lists every open job at once; there is no paging (``?page=2`` answers
the same page). The Next.js payload the page carries holds the whole list as a
structured ``"jobs"`` array, with each job's offices, workplace type, launch
date and full description. A long description is not in the array itself but in
a text chunk of the payload that the array points to (``"$20"``). A chunk is
written ``20:T11ca,`` followed by exactly 0x11ca bytes of UTF-8, and chunks
follow one another with no separator.

The cards of the rendered page link every job (``/{company}/{id}-{title}``).
That is a count independent of the payload: when the two disagree the read is
not complete and the scraper raises rather than return part of a board.

An unknown company is HTTP 404. ``company_slug`` is the company name in the
address (``hire`` for ``careers.kula.ai/hire``). ``ats_id`` is the job number.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._job_places import distinct_places, job_location, workplace_type
from ats_scrapers.scrapers._posting_detail import body_text
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.models import JobLocation

HOST = "careers.kula.ai"
MAX_DESCRIPTION_CHARS = 25_000

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.DOTALL)
_ORGANIZATION_RE = re.compile(r'"@type"\s*:\s*"Organization"\s*,\s*"name"\s*:\s*("(?:[^"\\]|\\.)*")')
_TITLE_RE = re.compile(r"<title>\s*(?:Jobs at|Careers @)\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
_CHUNK_START = re.compile(rb"(?:^|\n)(?=[0-9a-f]+:T[0-9a-f]+,)")
_CHUNK_HEAD = re.compile(rb"([0-9a-f]+):T([0-9a-f]+),")
_REFERENCE = re.compile(r"\$([0-9a-f]+)")
_EMPLOYMENT: dict[str, EmploymentType] = {
    "full_time": "FULL_TIME", "part_time": "PART_TIME", "contract": "CONTRACT",
    "intern": "INTERN", "internship": "INTERN", "temporary": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.KULA)
class KulaScraper(BaseScraper):
    """All open jobs of one Kula career site."""

    ats = ATSType.KULA

    def __init__(self, company_slug: str, **kwargs: Any) -> None:
        super().__init__(company_slug, **kwargs)
        slug = company_slug.strip().strip("/")
        if not _SLUG_RE.match(slug):
            raise ScraperError(
                f"KulaScraper: invalid company {company_slug!r} — expected the name in "
                f"https://{HOST}/<company>",
            )
        self.company_slug = slug

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            page = await fetch.get_text(f"https://{HOST}/{self.company_slug}")
        return self._parse(page)

    def _parse(self, page: str) -> list[Job]:
        payload = _payload(page)
        start = payload.find('"jobs":[')
        if start < 0:
            raise ScraperError(f"Kula ({self.company_slug}) returned an unrecognized page")
        try:
            items, _ = json.JSONDecoder().raw_decode(payload[start + len('"jobs":'):])
        except ValueError as exc:
            raise ScraperError(f"Kula ({self.company_slug}) jobs list is not readable") from exc
        if not isinstance(items, list):
            raise ScraperError(f"Kula ({self.company_slug}) jobs list is not a list")
        texts = _text_chunks(payload)
        company = _company(page) or self.company_slug
        jobs: dict[str, Job] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("listed") is False \
                    or item.get("on_career_page") is False:
                continue  # not on the public page, and no card either
            job = self._job(item, company, texts)
            if job is not None:
                jobs.setdefault(job.ats_id or "", job)
        linked = set(re.findall(
            rf'href="/{re.escape(self.company_slug)}/(\d+)', page, re.IGNORECASE,
        ))
        if linked != set(jobs):
            raise ScraperError(
                f"Kula ({self.company_slug}): the page links {len(linked)} jobs but its "
                f"data holds {len(jobs)}",
            )
        return list(jobs.values())

    def _job(self, item: dict[str, Any], company: str, texts: dict[str, str]) -> Job | None:
        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            return None
        detail: dict[str, Any] = item["ats_job"] if isinstance(item.get("ats_job"), dict) else {}
        offices = [o for o in detail.get("offices") or [] if isinstance(o, dict)]
        places: list[JobLocation | None] = [
            job_location(
                office.get("location"), city=office.get("city"), region=office.get("state"),
                country=office.get("country"), country_code=office.get("country_code"),
            )
            for office in offices
        ]
        locations = distinct_places(places)
        mode = workplace_type(str(detail.get("workplace") or "").replace("office", "onsite"))
        if mode is None and offices and all(o.get("remote") is True for o in offices):
            mode = "remote"
        codes = {p.country_iso for p in locations or [] if p.country_iso}
        description = detail.get("job_description")
        reference = _REFERENCE.fullmatch(description) if isinstance(description, str) else None
        if reference:
            description = texts.get(reference.group(1))
        department = detail.get("ats_department")
        url = f"https://{HOST}/{self.company_slug}/{job_id}"
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=self.ats,
            ats_id=job_id,
            location="; ".join(p.label for p in locations) if locations else None,
            locations=locations,
            country_iso=next(iter(codes)) if len(codes) == 1 else None,
            workplace_type=mode,
            is_remote=True if mode == "remote" else None,
            employment_type=_EMPLOYMENT.get(str(detail.get("employment_type") or "").lower()),
            department=department.get("name") if isinstance(department, dict) else None,
            apply_url=url,
            description=body_text(description)[:MAX_DESCRIPTION_CHARS] or None,
            posted_at=_instant(item.get("launch_at")),
            fetched_at=datetime.now(UTC),
        )


def _payload(page: str) -> str:
    """The Next.js flight payload of a page, its script pieces joined."""
    try:
        return "".join(json.loads(f'"{piece}"') for piece in _PUSH_RE.findall(page))
    except ValueError as exc:
        raise ScraperError("Kula page payload is not readable") from exc


def _text_chunks(payload: str) -> dict[str, str]:
    """``{id: text}`` of the payload's text chunks (``id:T<hex length>,<that many bytes>``)."""
    data, chunks, position = payload.encode(), {}, 0
    while (found := _CHUNK_START.search(data, position)) is not None:
        position = found.end()
        while (head := _CHUNK_HEAD.match(data, position)) is not None:
            end = head.end() + int(head.group(2), 16)
            chunks[head.group(1).decode()] = data[head.end():end].decode("utf-8", "replace")
            position = end
    return chunks


def _company(page: str) -> str | None:
    found = _ORGANIZATION_RE.search(page)
    if found:
        try:
            name = " ".join(str(json.loads(found.group(1))).split())
        except ValueError:
            name = ""
        if name:
            return name
    title = _TITLE_RE.search(page)
    return " ".join(title.group(1).split()) or None if title else None


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
