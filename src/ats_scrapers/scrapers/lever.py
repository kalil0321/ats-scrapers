"""Lever scraper.

Lever exposes a public JSON board at:
    https://api.lever.co/v0/postings/{slug}?mode=json

Single fetch returns every posting with rich fields:

* ``descriptionPlain`` — full plain-text job body (intro + sections).
* ``categories.commitment`` — "Full-time" / "Part-time" / "Internship" /
  "Contract"; we map to the canonical employment-type enum and keep
  the original string in ``commitment``.
* ``salaryRange`` — ``{min, max, currency, interval}`` when published.
* ``workplaceType`` — "remote" / "hybrid" / "onsite" → ``is_remote``.

No per-job fetch needed.

Lever hosts its customers in two regions that do not share data. A board
on ``jobs.lever.co`` is served by ``api.lever.co``; a board on
``jobs.eu.lever.co`` is served by ``api.eu.lever.co`` and answers 404 on
the other API. A slug says nothing about its region, so the region is given
with the slug: ``LeverScraper("acme", region="eu")``, or the board URL as
the slug (``https://jobs.eu.lever.co/acme``). A bare slug is the US region.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_TEMPLATE = "https://{host}/v0/postings/{slug}?mode=json"
DEFAULT_REGION = "us"
# The two regions, each with its API host and its board host.
API_HOSTS = {"us": "api.lever.co", "eu": "api.eu.lever.co"}
BOARD_HOSTS = {"jobs.lever.co": "us", "jobs.eu.lever.co": "eu"}

_LEVER_INTERVAL_MAP = {
    "1-YEAR": "YEAR",
    "PER-YEAR-SALARY": "YEAR",
    "1-MONTH": "MONTH",
    "1-WEEK": "WEEK",
    "1-DAY": "DAY",
    "1-HOUR": "HOUR",
    "PER-HOUR-WAGE": "HOUR",
    "YEAR": "YEAR",
    "MONTH": "MONTH",
    "WEEK": "WEEK",
    "DAY": "DAY",
    "HOUR": "HOUR",
}

# ``categories.commitment`` is a freeform string set by the employer.
# Map common variants to the canonical employment-type enum; keep the
# original string in ``commitment`` for downstream display.
_COMMITMENT_TO_EMPLOYMENT_TYPE = {
    "full-time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "full time": "FULL_TIME",
    "regular": "FULL_TIME",
    "part-time": "PART_TIME",
    "parttime": "PART_TIME",
    "part time": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "consultant": "CONTRACT",
    "freelance": "CONTRACT",
    "fixed-term": "CONTRACT",
    "internship": "INTERN",
    "intern": "INTERN",
    "co-op": "INTERN",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
    "seasonal": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.LEVER)
class LeverScraper(BaseScraper):
    """``company_slug`` is the board slug (``acme``) or the board URL
    (``https://jobs.eu.lever.co/acme``). ``region`` is ``"us"`` or ``"eu"``;
    left out, it is read from a board URL, else it is the US region."""

    ats = ATSType.LEVER

    def __init__(
        self,
        company_slug: str,
        *,
        region: str | None = None,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        try:
            self.tenant, self.region = _parse_slug(company_slug, region)
        except ValueError as exc:
            raise ScraperError(str(exc)) from exc

    async def afetch(self) -> list[Job]:
        url = API_TEMPLATE.format(
            host=API_HOSTS[self.region], slug=quote(self.tenant, safe="")
        )
        async with self.make_fetcher() as fetch:
            try:
                payload = await fetch.get_json(url)
            except CompanyNotFoundError as exc:
                if self.region != DEFAULT_REGION:
                    raise
                # The other region is not tried: the same slug can be
                # another company there.
                raise CompanyNotFoundError(
                    f"{exc} (no such board in Lever's US region; a board on "
                    f"jobs.eu.lever.co needs region='eu')"
                ) from exc
        return [self._parse_job(item) for item in payload]

    def _parse_job(self, item: dict[str, Any]) -> Job:
        categories = item.get("categories") or {}
        commitment = categories.get("commitment")
        salary_range = item.get("salaryRange") or {}
        salary_min = salary_range.get("min")
        salary_max = salary_range.get("max")
        salary_currency = salary_range.get("currency")
        salary_interval = (salary_range.get("interval") or "").upper()
        salary_period = _LEVER_INTERVAL_MAP.get(salary_interval)

        # Map the freeform ``commitment`` text to our canonical
        # employment-type enum. Keep the original in ``commitment``.
        employment_type: str | None = None
        if isinstance(commitment, str):
            norm = commitment.strip().lower()
            for key, mapped in _COMMITMENT_TO_EMPLOYMENT_TYPE.items():
                if key in norm:
                    employment_type = mapped
                    break

        # Description assembly. Lever's API exposes the body across multiple
        # fields: ``description`` (intro HTML) plus a separate ``lists``
        # array carrying the structured sections (Responsibilities,
        # Requirements, Compensation Details, etc.) as HTML chunks. The
        # ``descriptionPlain`` text-only field is shorter than ``description``
        # AND completely omits ``lists`` content, so previously preferring it
        # silently dropped 50–80% of each posting's body.
        #
        # Now we always concatenate the HTML intro with each lists section
        # (prefixing the section's heading), and rely on the post-scrape
        # markdownify step in scripts/normalize_descriptions.py to render
        # the assembled HTML to clean markdown.
        intro_html = item.get("description") or ""
        if not isinstance(intro_html, str):
            intro_html = ""
        sections = item.get("lists") or []
        parts: list[str] = []
        if intro_html.strip():
            parts.append(intro_html)
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = (section.get("text") or "").strip()
            content = (section.get("content") or "").strip()
            if not content:
                continue
            if heading:
                parts.append(f"<h3>{heading}</h3>\n{content}")
            else:
                parts.append(content)
        description: str | None = None
        if parts:
            assembled = "\n\n".join(parts)
            description = assembled.strip()[:25_000] or None
        elif isinstance(item.get("descriptionPlain"), str):
            # Last-ditch fallback: the legacy plain-text field. Rare —
            # only fires when both ``description`` and ``lists`` are empty.
            description = item["descriptionPlain"].strip()[:25_000] or None

        raw: dict[str, Any] = {}
        if categories:
            raw["categories"] = categories
        for k in ("workplaceType", "country", "tags", "additionalPlain"):
            v = item.get(k)
            if v:
                raw[k] = v

        is_remote = None
        wp = (item.get("workplaceType") or "").lower()
        if wp == "remote":
            is_remote = True
        elif wp in {"on-site", "onsite", "in-office"}:
            is_remote = False
        # ``hybrid`` stays None — neither purely remote nor onsite.

        return Job(
            url=item["hostedUrl"],
            title=item["text"],
            company=self.tenant,
            ats_type=ATSType.LEVER,
            ats_id=item["id"],
            location=categories.get("location"),
            country_iso=_country_iso(item.get("country")),
            department=categories.get("department"),
            team=categories.get("team"),
            commitment=commitment,
            employment_type=employment_type,
            description=description,
            apply_url=item.get("applyUrl"),
            is_remote=is_remote,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=salary_currency,
            salary_period=salary_period,
            posted_at=_parse_ms(item.get("createdAt")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _parse_slug(value: str, region: str | None) -> tuple[str, str]:
    """``(tenant, region)`` from a board slug or a board URL."""
    raw = (value or "").strip()
    wanted = (region or "").strip().lower() or None
    if wanted is not None and wanted not in API_HOSTS:
        raise ValueError(
            f"Lever region must be one of {sorted(API_HOSTS)}, got {region!r}"
        )
    if "://" not in raw:
        if not raw:
            raise ValueError("Lever needs a board slug or a board URL")
        return raw, wanted or DEFAULT_REGION
    parsed = urlparse(raw)
    found = BOARD_HOSTS.get((parsed.hostname or "").lower())
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
        or found is None
        or not segments
    ):
        raise ValueError(
            f"Lever board URL must be https://jobs.lever.co/{{slug}} or "
            f"https://jobs.eu.lever.co/{{slug}}, got {value!r}"
        )
    if wanted is not None and wanted != found:
        raise ValueError(
            f"Lever board URL {value!r} is in region {found!r}, "
            f"not {wanted!r}"
        )
    return unquote(segments[0]), found


def _parse_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OSError):
        return None


def _country_iso(value: object) -> str | None:
    """Lever's posting ``country`` is an ISO 3166-1 alpha-2 code or null."""
    if not isinstance(value, str):
        return None
    code = value.strip()
    # ASCII letters only, checked before uppercasing: "ß".upper() is "SS".
    return code.upper() if len(code) == 2 and code.isascii() and code.isalpha() else None
