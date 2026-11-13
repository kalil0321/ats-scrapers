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
scraper itself wants the whole URL. Taleo Enterprise is host plus career
section (``valero.taleo.net/2``).

Custom-domain career sites (careers.example.com fronting Phenom,
Avature, or Eightfold) cannot be recognized from the URL alone —
``resolve_careers_url`` returns ``None`` and
``get_scraper_for_url`` raises with guidance.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import ParseResult, quote, unquote, urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType

if TYPE_CHECKING:
    from ats_scrapers.scrapers.base import BaseScraper

# Tenant slugs interpolated into hostnames/paths must look like DNS
# labels / board identifiers — this also refuses anything that could
# escape the intended host when a scraper interpolates the slug into
# an f-string URL.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*$", re.IGNORECASE)
# Lever board slugs also use "&" (``1840&Company``). The Lever scraper
# escapes the slug in the API URL, so the wider set is safe there.
_LEVER_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._&\-]*", re.IGNORECASE)
_LEVER_REGIONS = {"jobs.lever.co": "us", "jobs.eu.lever.co": "eu"}
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
# A CATS portal page: /careers/{portal number}, then "-Name" or "/jobs/...".
_CATSONE_PORTAL_PATH_RE = re.compile(r"^/careers/(\d{1,12})(?:[-/]|$)")
_DAYFORCE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_HOTJOB_HOST_RE = re.compile(r"(?:[a-z0-9-]+\.)*hotjob\.cn", re.IGNORECASE)
_HOTJOB_SUITE_RE = re.compile(r"SU[a-f0-9]{24}", re.IGNORECASE)
_HOTJOB_TENANT_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?",
    re.IGNORECASE,
)


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
    "apply.clearcompany.com": ATSType.CLEARCOMPANY,
}

# Subdomain tenants: https://{slug}.{suffix}/...
_SUBDOMAIN_SUFFIXES: dict[str, ATSType] = {
    ".applicantpro.com": ATSType.APPLICANTPRO,
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
    ".career.softgarden.de": ATSType.SOFTGARDEN,
    # ClearCompany's earlier address; {site}.hrmdirect.com redirects to
    # apply.clearcompany.com/{site}.
    ".hrmdirect.com": ATSType.CLEARCOMPANY,
    ".peopleadmin.com": ATSType.PEOPLEADMIN,
    ".talent-soft.com": ATSType.TALENTSOFT,
}

# Full-URL tenants: the scraper's slug IS the careers URL.
_WORKDAY_HOST_RE = re.compile(r"^[^.]+\.wd\d+\.myworkdayjobs\.com$")
_TALEO_HOST_RE = re.compile(r"^[^.]+\.tbe\.taleo\.net$")
# Taleo Enterprise: one label before taleo.net ({zone}.taleo.net), with the
# career section as the second path segment.
_TALEO_ENTERPRISE_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.taleo\.net$", re.IGNORECASE
)
# The whole path: the section, then at most one page name ("jobsearch.ftl").
_TALEO_ENTERPRISE_PATH_RE = re.compile(
    r"^/careersection/(?P<section>[A-Za-z0-9][A-Za-z0-9_-]{0,79})"
    r"(?:/(?:[A-Za-z0-9_-]+\.[A-Za-z0-9]+)?)?$"
)
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
_PAYCOM_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_KEKA_PORTAL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)
_KEKA_RESERVED_SEGMENTS = frozenset(
    {"api", "applyjob", "content", "jobdetails"}
)
_APPLITRACK_TENANT_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def resolve_careers_url(url: str) -> ResolvedCareersUrl | None:
    """Map a public careers URL to ``(ats, slug)``.

    Returns ``None`` when the URL doesn't match any known ATS shape
    (typically a custom-domain careers site). The returned slug is in
    the exact form the matching scraper's constructor expects.
    """
    parsed = urlparse(url if "//" in url else f"https://{url}")
    input_host = (parsed.hostname or "").lower()
    host = input_host
    if not host:
        return None
    if host.startswith("www."):
        host = host.removeprefix("www.")

    # One host label at most before the domain, as the scraper accepts:
    # ``foo.bar.applitrack.com`` is not a board.
    if host == "applitrack.com" or (
        host.endswith(".applitrack.com")
        and _APPLITRACK_TENANT_RE.fullmatch(host.removesuffix(".applitrack.com"))
    ):
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 2
            and _APPLITRACK_TENANT_RE.fullmatch(segments[0])
            and segments[1].casefold() == "onlineapp"
        ):
            tenant = segments[0].casefold()
            return ResolvedCareersUrl(
                ATSType.APPLITRACK,
                f"https://www.applitrack.com/{tenant}/onlineapp",
            )
        return None

    if host in _LEVER_REGIONS:
        segment = _first_path_segment(parsed)
        slug = unquote(segment) if segment else ""
        # fullmatch: "$" would also accept a decoded trailing newline.
        if not _LEVER_SLUG_RE.fullmatch(slug):
            return None
        if _LEVER_REGIONS[host] == "eu":
            # Lever's EU region has its own API, and a bare slug is read
            # as a US board: the slug is the board URL.
            return ResolvedCareersUrl(
                ATSType.LEVER, f"https://jobs.eu.lever.co/{quote(slug, safe='')}"
            )
        return ResolvedCareersUrl(ATSType.LEVER, slug)

    if host in _PATH_HOSTS:
        slug = _first_path_segment(parsed)
        if slug and _SLUG_RE.match(slug):
            return ResolvedCareersUrl(_PATH_HOSTS[host], slug)
        return None

    if host == "herp.careers":
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 2 and segments[0] == "v1" and _DNS_LABEL_RE.fullmatch(segments[1]):
            return ResolvedCareersUrl(ATSType.HERP, segments[1])
        return None

    if host.endswith(".varbi.com"):
        # Read the tenant from the host as given: ``www.`` was removed
        # above, and ``www.acme.varbi.com`` is not a tenant's board.
        slug = (parsed.hostname or "").lower().removesuffix(".varbi.com")
        if _DNS_LABEL_RE.fullmatch(slug) and slug not in {"api", "support", "login"}:
            return ResolvedCareersUrl(ATSType.VARBI, slug)
        return None

    if host == "hrmos.co":
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 2
            and segments[0] == "pages"
            and _DNS_LABEL_RE.fullmatch(segments[1])
        ):
            return ResolvedCareersUrl(ATSType.HRMOS, segments[1])
        return None

    if host.endswith(".keka.com"):
        tenant = host.removesuffix(".keka.com")
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            _DNS_LABEL_RE.fullmatch(tenant)
            and segments
            and segments[0].casefold() == "careers"
        ):
            portal = "default"
            if (
                len(segments) >= 2
                and segments[1].casefold() not in _KEKA_RESERVED_SEGMENTS
                and _KEKA_PORTAL_RE.fullmatch(segments[1])
            ):
                portal = segments[1].casefold()
            valid_remainder = (
                len(segments) == 1
                or (
                    portal == "default"
                    and len(segments) == 3
                    and segments[1].casefold() in {"applyjob", "jobdetails"}
                    and segments[2].isdigit()
                )
                or (
                    portal != "default"
                    and (
                        len(segments) == 2
                        or (
                            len(segments) == 4
                            and segments[2].casefold()
                            in {"applyjob", "jobdetails"}
                            and segments[3].isdigit()
                        )
                    )
                )
            )
            if valid_remainder:
                portal_url = (
                    f"https://{host}/careers"
                    if portal == "default"
                    else f"https://{host}/careers/{portal}"
                )
                return ResolvedCareersUrl(ATSType.KEKA, portal_url)
        return None

    if host in {"paycomonline.net", "www.paycomonline.net"}:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 5
            and segments[:4] == ["v4", "ats", "web.php", "portal"]
            and _PAYCOM_TOKEN_RE.fullmatch(segments[4])
        ):
            return ResolvedCareersUrl(ATSType.PAYCOM, segments[4].lower())
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

    if _HOTJOB_HOST_RE.fullmatch(host):
        portal_host = (
            input_host
            if _HOTJOB_HOST_RE.fullmatch(input_host)
            else host
        )
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments and _HOTJOB_SUITE_RE.fullmatch(segments[0]):
            return ResolvedCareersUrl(
                ATSType.WINTALENT,
                f"https://{portal_host}/{segments[0]}",
            )
        if (
            len(segments) >= 2
            and segments[0].casefold() == "wt"
            and _HOTJOB_TENANT_RE.fullmatch(segments[1])
        ):
            return ResolvedCareersUrl(
                ATSType.WINTALENT,
                f"https://{portal_host}/wt/{segments[1]}",
            )
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

    if host.endswith(".catsone.com"):
        # A tenant can have several portals: keep the number of the one
        # the URL names, so that the scraper reads that portal.
        tenant = host.removesuffix(".catsone.com")
        if not _DNS_LABEL_RE.fullmatch(tenant):
            return None
        portal = _CATSONE_PORTAL_PATH_RE.match(parsed.path)
        if portal:
            return ResolvedCareersUrl(ATSType.CATSONE, f"{tenant}/{portal.group(1)}")
        # The tenant's own address, which leads to its default portal. Any
        # other path names a page this does not know how to read as a
        # portal, and is not taken for the default one.
        if parsed.path.rstrip("/") in ("", "/careers"):
            return ResolvedCareersUrl(ATSType.CATSONE, tenant)
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

    if _TALEO_ENTERPRISE_HOST_RE.match(host):
        # Taleo Enterprise: the slug is "{zone}.taleo.net/{section}". A zone
        # URL without a career section cannot be scraped ("rest" is the
        # API path, not a section).
        section = _TALEO_ENTERPRISE_PATH_RE.match(parsed.path)
        if section and section.group("section") != "rest":
            return ResolvedCareersUrl(
                ATSType.TALEO_ENTERPRISE,
                f"{host.lower()}/{section.group('section')}",
            )
        return None

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
