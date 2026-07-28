"""Resolve a careers-page URL to a scraper — no ATS knowledge required.

Almost nobody knows which ATS a company uses ("OpenAI is on Ashby" is
recruiting-insider trivia), but anyone can Google "<company> careers"
and paste the URL. Every multi-tenant ATS serves tenants from a
recognizable URL shape, so the mapping is deterministic and offline:

    >>> from ats_scrapers import get_scraper_for_url
    >>> scraper = get_scraper_for_url("https://jobs.ashbyhq.com/openai")
    >>> jobs = scraper.fetch()

Each pattern below mirrors the slug contract documented in the
corresponding scraper module — path-based tenants (Greenhouse, Lever,
Ashby, …), subdomain tenants (Recruitee, Teamtailor, Breezy, …), and
full-URL tenants (Workday, Taleo, and iCIMS variants) where the
scraper itself wants the whole URL.

Custom-domain career sites (careers.example.com fronting Phenom,
Avature, or Eightfold) cannot be recognized from the URL alone —
``resolve_careers_url`` returns ``None`` and
``get_scraper_for_url`` raises with guidance.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import ParseResult, urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType

if TYPE_CHECKING:
    from ats_scrapers.scrapers.base import BaseScraper

# Tenant slugs interpolated into hostnames/paths must look like DNS
# labels / board identifiers — this also refuses anything that could
# escape the intended host when a scraper interpolates the slug into
# an f-string URL.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*$", re.IGNORECASE)
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_DAYFORCE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ResolvedCareersUrl(NamedTuple):
    """Outcome of :func:`resolve_careers_url`."""

    ats: ATSType
    slug: str


def _first_path_segment(parsed: ParseResult) -> str | None:
    segments = [s for s in parsed.path.split("/") if s]
    return segments[0] if segments else None


# Path-based tenants: https://{host}/{slug}/...
_PATH_HOSTS: dict[str, ATSType] = {
    "boards.greenhouse.io": ATSType.GREENHOUSE,
    "job-boards.greenhouse.io": ATSType.GREENHOUSE,
    "boards.eu.greenhouse.io": ATSType.GREENHOUSE,
    "job-boards.eu.greenhouse.io": ATSType.GREENHOUSE,
    "jobs.lever.co": ATSType.LEVER,
    "jobs.eu.lever.co": ATSType.LEVER,
    "jobs.ashbyhq.com": ATSType.ASHBY,
    "apply.workable.com": ATSType.WORKABLE,
    "careers.smartrecruiters.com": ATSType.SMARTRECRUITERS,
    "jobs.smartrecruiters.com": ATSType.SMARTRECRUITERS,
    "jobs.gem.com": ATSType.GEM,
    "ats.rippling.com": ATSType.RIPPLING,
}

# Subdomain tenants: https://{slug}.{suffix}/...
_SUBDOMAIN_SUFFIXES: dict[str, ATSType] = {
    ".recruitee.com": ATSType.RECRUITEE,
    ".teamtailor.com": ATSType.TEAMTAILOR,
    ".breezy.hr": ATSType.BREEZY,
    ".bamboohr.com": ATSType.BAMBOOHR,
    ".gupy.io": ATSType.GUPY,
    ".jobs.personio.com": ATSType.PERSONIO,
    ".jobs.personio.de": ATSType.PERSONIO,
    ".pinpointhq.com": ATSType.PINPOINT,
    ".applytojob.com": ATSType.JAZZHR,
    ".avature.net": ATSType.AVATURE,
    ".eightfold.ai": ATSType.EIGHTFOLD,
}

# Full-URL tenants: the scraper's slug IS the careers URL.
_WORKDAY_HOST_RE = re.compile(r"^[^.]+\.wd\d+\.myworkdayjobs\.com$")
_TALEO_HOST_RE = re.compile(r"^[^.]+\.tbe\.taleo\.net$")
_ICIMS_HOST_RE = re.compile(r"^(?P<prefix>[a-z0-9-]+)\.icims\.com$", re.IGNORECASE)
_JOBVITE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_JOBVITE_RESERVED_SEGMENTS = frozenset({"jobs", "search", "viewall"})
_PAGEUP_CLIENT_RE = re.compile(r"^\d+$")
_PAGEUP_SEGMENT_RE = re.compile(r"^[A-Za-z0-9-]+$")
_PAGEUP_LOCALE_RE = re.compile(r"^[A-Za-z]{2}(?:-[A-Za-z]{2})?$")
_UKG_HOST_RE = re.compile(r"^recruiting\d*\.ultipro\.(?:com|ca)$", re.IGNORECASE)
_UKG_TENANT_RE = re.compile(r"^[A-Za-z0-9]+$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def resolve_careers_url(url: str) -> ResolvedCareersUrl | None:
    """Map a public careers URL to ``(ats, slug)``.

    Returns ``None`` when the URL doesn't match any known ATS shape
    (typically a custom-domain careers site). The returned slug is in
    the exact form the matching scraper's constructor expects.
    """
    parsed = urlparse(url if "//" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host.removeprefix("www.")

    if host in _PATH_HOSTS:
        slug = _first_path_segment(parsed)
        if slug and _SLUG_RE.match(slug):
            return ResolvedCareersUrl(_PATH_HOSTS[host], slug)
        return None

    if host == "jobs.jobvite.com":
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 2
            and segments[0] == "careers"
            and segments[1].casefold() not in _JOBVITE_RESERVED_SEGMENTS
            and _JOBVITE_SEGMENT_RE.fullmatch(segments[1])
        ):
            return ResolvedCareersUrl(
                ATSType.JOBVITE,
                f"careers/{segments[1]}",
            )
        if (
            segments
            and segments[0] != "careers"
            and segments[0].casefold() not in _JOBVITE_RESERVED_SEGMENTS
            and _JOBVITE_SEGMENT_RE.fullmatch(segments[0])
        ):
            return ResolvedCareersUrl(ATSType.JOBVITE, segments[0])
        return None

    if host == "careers.pageuppeople.com":
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments and segments[0].lower() == "mob":
            segments.pop(0)
        if (
            len(segments) >= 3
            and _PAGEUP_CLIENT_RE.fullmatch(segments[0])
            and _PAGEUP_SEGMENT_RE.fullmatch(segments[1])
            and _PAGEUP_LOCALE_RE.fullmatch(segments[2])
        ):
            return ResolvedCareersUrl(
                ATSType.PAGEUP,
                "/".join(segments[:3]),
            )
        return None

    if _UKG_HOST_RE.fullmatch(host):
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 3
            and _UKG_TENANT_RE.fullmatch(segments[0])
            and segments[1].casefold() == "jobboard"
            and _UUID_RE.fullmatch(segments[2])
        ):
            return ResolvedCareersUrl(
                ATSType.UKG,
                f"https://{host}/{segments[0]}/JobBoard/{segments[2]}",
            )
        return None

    if host == "recruiting.paylocity.com":
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) == 4
            and [segment.casefold() for segment in segments[:3]]
            == ["recruiting", "jobs", "all"]
            and _UUID_RE.fullmatch(segments[3])
            and not parsed.query
            and not parsed.fragment
        ):
            return ResolvedCareersUrl(ATSType.PAYLOCITY, segments[3].lower())
        return None

    # join.com/companies/{slug}
    if host == "join.com":
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) >= 2 and segments[0] == "companies" and _SLUG_RE.match(segments[1]):
            return ResolvedCareersUrl(ATSType.JOIN_COM, segments[1])
        return None

    if host in {"app.mokahr.com", "hire-r1.mokahr.com"}:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 3
            and segments[0] in {"social-recruitment", "campus-recruitment"}
            and _SLUG_RE.match(segments[1])
            and segments[2].isdigit()
        ):
            prefix = "hire-r1/" if host == "hire-r1.mokahr.com" else ""
            recruitment_type = (
                "/campus" if segments[0] == "campus-recruitment" else ""
            )
            return ResolvedCareersUrl(
                ATSType.MOKA,
                f"{prefix}{segments[1]}/{segments[2]}{recruitment_type}",
            )
        return None

    if host == "jobs.dayforcehcm.com":
        segments = [segment for segment in parsed.path.split("/") if segment]

        def resolve_segments(parts: list[str]) -> str | None:
            if (
                len(parts) < 2
                or not _DAYFORCE_SEGMENT_RE.fullmatch(parts[0])
                or not _DAYFORCE_SEGMENT_RE.fullmatch(parts[1])
            ):
                return None
            remainder = [segment.casefold() for segment in parts[2:]]
            if remainder and not (
                remainder[0] == "jobs"
                and len(remainder) in {1, 2, 3}
                and (len(remainder) == 1 or remainder[1].isdigit())
                and (len(remainder) < 3 or remainder[2] == "apply")
            ):
                return None
            return f"{parts[0]}/{parts[1]}"

        if (slug := resolve_segments(segments)) is not None:
            return ResolvedCareersUrl(ATSType.DAYFORCE, slug)
        if (
            len(segments) >= 3
            and re.fullmatch(
                r"[A-Za-z]{2}(?:-[A-Za-z]{2})?",
                segments[0],
            )
            and (slug := resolve_segments(segments[1:])) is not None
        ):
            return ResolvedCareersUrl(ATSType.DAYFORCE, slug)
        return None

    for suffix, tld in ((".darwinbox.in", "in"), (".darwinbox.com", "com")):
        if host.endswith(suffix):
            tenant = host.removesuffix(suffix)
            if _DNS_LABEL_RE.fullmatch(tenant):
                slug = tenant if tld == "in" else f"{tenant}.com"
                return ResolvedCareersUrl(ATSType.DARWINBOX, slug)
            return None

    if host.endswith(".zhiye.com"):
        tenant = host.removesuffix(".zhiye.com")
        if _DNS_LABEL_RE.fullmatch(tenant):
            segments = [segment for segment in parsed.path.split("/") if segment]
            is_legacy = bool(
                segments
                and (
                    segments[0].casefold() in {"index", "zpdetail", "zwxq"}
                    or (
                        segments[0].casefold() in {"social", "campus", "intern"}
                        and not (
                            len(segments) > 1
                            and segments[1].casefold() == "jobs"
                        )
                    )
                )
            )
            ats = ATSType.BEISEN_LEGACY if is_legacy else ATSType.BEISEN
            return ResolvedCareersUrl(ats, tenant)
        return None

    for suffix, ats in _SUBDOMAIN_SUFFIXES.items():
        if host.endswith(suffix):
            slug = host.removesuffix(suffix)
            if slug and "." not in slug and _SLUG_RE.match(slug):
                return ResolvedCareersUrl(ats, slug)
            return None

    if _WORKDAY_HOST_RE.match(host):
        # WorkdayScraper wants the full careers URL (tenant, wdN
        # instance, and site name are all load-bearing).
        return ResolvedCareersUrl(ATSType.WORKDAY, f"https://{host}{parsed.path}")

    if _TALEO_HOST_RE.match(host):
        # Taleo TBE wants the full search-results URL including query.
        query = f"?{parsed.query}" if parsed.query else ""
        return ResolvedCareersUrl(ATSType.TALEO, f"https://{host}{parsed.path}{query}")

    icims = _ICIMS_HOST_RE.match(host)
    if icims:
        prefix = icims.group("prefix").lower()
        if prefix.startswith("careers-"):
            return ResolvedCareersUrl(ATSType.ICIMS, prefix.removeprefix("careers-"))
        # Non-standard portal prefixes (uscareers-..., jobs-...): the
        # scraper accepts the full portal URL verbatim.
        return ResolvedCareersUrl(ATSType.ICIMS, f"https://{host}")

    return None


def get_scraper_for_url(url: str, **kwargs: object) -> BaseScraper:
    """Build a ready-to-use scraper from a public careers URL.

    Keyword arguments are forwarded to the scraper constructor
    (``timeout``, ``include_descriptions``, ``proxy``, …).

    Raises :class:`ScraperError` for URLs that don't match a known ATS
    shape — custom-domain careers sites need the explicit
    ``get_scraper(ats, slug)`` path.
    """
    resolved = resolve_careers_url(url)
    if resolved is None:
        raise ScraperError(
            f"Could not recognize an ATS from {url!r}. Custom-domain careers "
            f"sites can't be resolved from the URL alone — if you know the "
            f"platform, use get_scraper(ats, slug), or look the company up "
            f"with find_company()."
        )
    # Import the scrapers package (not just .base): importing it runs
    # every @ScraperRegistry.register decorator, so the registry is
    # populated even when the caller only imported the package root.
    from ats_scrapers.scrapers import get_scraper

    return get_scraper(resolved.ats, resolved.slug, **kwargs)
