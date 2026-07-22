"""Manfred (https://www.getmanfred.com) — Spanish-speaking dev jobs.

Manfred is a direct-posting tech-jobs board for Spanish-speaking
markets (Spain, Latin America). Companies pay to list — not LinkedIn /
Indeed syndication. Coverage is small but high-signal: ~1,500 active
postings, all developer-focused, all with structured salary +
location + remote-percentage data.

Public REST API at ``https://www.getmanfred.com/api/v2/public/offers``.
The ``lang`` query param is required (must be ``EN`` or ``ES``); we
default to ``EN`` so titles are in English when the company provided
a translation, and fall back to the Spanish original otherwise.

The endpoint returns the entire active board in a single response (no
pagination). Single-source scraper: ``company_slug`` is informational
and ignored.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_URL = "https://www.getmanfred.com/api/v2/public/offers"
DETAIL_URL_TEMPLATE = "https://www.getmanfred.com/api/v2/public/offers/{offer_id}"
JOB_URL_TEMPLATE = "https://www.getmanfred.com/job-offers/{slug}"
DEFAULT_LANG = "EN"
DETAIL_CONCURRENCY = 6

# Manfred's currency field is the human symbol ('€', '$', '£'); map
# to ISO 4217 codes our Job model expects.
_CURRENCY_MAP: dict[str, str] = {
    "€": "EUR",
    "$": "USD",
    "£": "GBP",
    "¥": "JPY",
    "₣": "CHF",
    "kr": "SEK",
}


@ScraperRegistry.register(ATSType.MANFRED)
class ManfredScraper(BaseScraper):
    """Manfred (getmanfred.com) — Spanish-speaking dev jobs.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``).

    Knobs:
    - ``lang`` — ``"EN"`` (default) or ``"ES"``. The API requires one
      of these and the response language follows. Most postings are
      EN-localized so the default is the safer pick for cross-source
      consistency.
    """

    ats = ATSType.MANFRED
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 60.0,  # API can take ~10s for the full payload.
        include_descriptions: bool = True,
        proxy: str | None = None,
        lang: str = DEFAULT_LANG,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.lang = lang.upper()
        if self.lang not in ("EN", "ES"):
            raise ScraperError(
                f"Manfred ``lang`` must be 'EN' or 'ES', got {lang!r}"
            )

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(fetch, sem, copy)
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            payload = await fetch.get_json(API_URL, params={"lang": self.lang})
            if not isinstance(payload, list):
                raise ScraperError(
                    f"Manfred API shape changed — expected a list, "
                    f"got {type(payload).__name__}"
                )
            seen: set[str] = set()
            jobs: list[Job] = []
            for item in payload:
                job = self._parse(item)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                jobs.append(job)
            if self.include_descriptions and jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(self._enrich_description(fetch, sem, j) for j in jobs))
        return jobs

    async def _enrich_description(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        offer_id = (job.raw or {}).get("id")
        if offer_id is None:
            return
        url = DETAIL_URL_TEMPLATE.format(offer_id=offer_id)
        async with sem:
            try:
                detail = await fetch.get_json(url, params={"lang": self.lang})
            except (ScraperError, ValueError):
                # Detail enrichment is best-effort — a missing or broken
                # detail endpoint must not sink the listing row.
                return
        description = _compose_description(detail)
        if description and not job.description:
            job.description = description[:25_000]

    def _parse(self, item: dict[str, Any]) -> Job | None:
        slug = (item.get("slug") or "").strip()
        title = (item.get("position") or "").strip()
        if not slug or not title:
            return None

        # ``status`` is one of ACTIVE / DRAFT / CLOSED — filter to
        # ACTIVE so consumers don't see closed/expired roles in the
        # output.
        if (item.get("status") or "").upper() != "ACTIVE":
            return None

        company = ((item.get("company") or {}).get("name") or "").strip() or "Unknown"
        location = _format_location(item.get("locations"))
        remote_pct = item.get("remotePercentage")
        # Manfred's ``remotePercentage`` is 0..100. We surface anything
        # >= 50 as remote (the field's semantics is 'how much of the
        # week the role can be remote') — common Manfred postings are
        # 50% / 80% / 100%.
        is_remote = (
            remote_pct >= 50 if isinstance(remote_pct, (int, float)) else None
        )

        salary_min = _to_pos_float(item.get("salaryFrom"))
        salary_max = _to_pos_float(item.get("salaryTo"))
        currency_symbol = item.get("currency") or "€"
        salary_currency = _CURRENCY_MAP.get(currency_symbol, currency_symbol[:3].upper())

        posted_at = _parse_iso(item.get("updatedAt"))

        raw: dict[str, Any] = {}
        if item.get("id") is not None:
            raw["id"] = item["id"]
        if isinstance(remote_pct, (int, float)):
            raw["remote_percentage"] = remote_pct
        if isinstance(item.get("offerLanguages"), list) and item["offerLanguages"]:
            raw["offer_languages"] = item["offerLanguages"]
        equity_inf = item.get("equityInf")
        equity_sup = item.get("equitySup")
        if isinstance(equity_inf, (int, float)) and equity_inf > 0:
            raw["equity_min"] = equity_inf
        if isinstance(equity_sup, (int, float)) and equity_sup > 0:
            raw["equity_max"] = equity_sup
        bonus = item.get("bonus")
        if isinstance(bonus, (int, float)) and bonus > 0:
            raw["bonus"] = bonus
        ic = item.get("internalCode")
        if ic:
            raw["internal_code"] = ic

        return Job(
            url=JOB_URL_TEMPLATE.format(slug=slug),
            title=title,
            company=company,
            ats_type=ATSType.MANFRED,
            ats_id=slug,
            location=location,
            is_remote=is_remote,
            salary_currency=salary_currency if (salary_min or salary_max) else None,
            salary_period="YEAR",
            salary_min=salary_min,
            salary_max=salary_max,
            requisition_id=ic if isinstance(ic, str) else None,
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _format_location(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    cleaned = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if not cleaned:
        return None
    return " | ".join(cleaned[:5])


def _to_pos_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _compose_description(item: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for key in (
        "introduction",
        "whatWillYouDo",
        "responsibilities",
        "howWillYouDoIt",
        "whatOffering",
        "whereWillDoIt",
        "whenWillDoIt",
        "whoWillDoItWith",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(_markdown_to_text(value))
        elif isinstance(value, list):
            cleaned = [
                _markdown_to_text(v)
                for v in value
                if isinstance(v, str) and v.strip()
            ]
            if cleaned:
                parts.append("\n".join(f"- {v}" for v in cleaned))
    text = "\n\n".join(p for p in parts if p).strip()
    return text or None


def _markdown_to_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
