"""Tenant-identifier validation for URL construction (GH-181).

Scrapers interpolate ``company_slug`` into URLs — most dangerously into
*hostnames* (``f"https://{slug}.recruitee.com"``). Without validation a
"slug" like ``"evil.com/x?y="`` produces a request whose host is
``evil.com``: server-side request forgery when slugs come from
untrusted input (e.g. community-contributed tenant CSVs run by the
publish pipeline).

Two validators, matching the two slug contracts scrapers document:

- :func:`require_host_label` for bare tenant slugs that become a DNS
  label of a fixed ATS domain;
- :func:`require_http_url` for scrapers that accept a full careers URL
  (custom-domain tenants) — scheme restricted to http(s) so a slug
  can't smuggle ``file://`` or other schemes into the HTTP client.

Call them at scraper construction time: bad input fails fast with a
clear message instead of producing a request to the wrong host.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError

# One DNS label: letters/digits with inner hyphens, max 63 chars.
# Deliberately no dots — a dot is exactly how a slug escapes the
# intended parent domain.
_HOST_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


def require_host_label(slug: str, *, provider: str) -> str:
    """Return ``slug`` stripped, or raise if it can't be a DNS label.

    For scrapers that build ``https://{slug}.<ats-domain>`` URLs.
    """
    cleaned = slug.strip()
    if not _HOST_LABEL_RE.match(cleaned):
        raise ScraperError(
            f"{provider}: invalid tenant slug {slug!r} — expected a bare "
            f"tenant name (letters, digits, hyphens), e.g. 'acme'. Slugs "
            f"containing dots, slashes, or other separators would change "
            f"the request host."
        )
    return cleaned


def require_http_url(url: str, *, provider: str) -> str:
    """Return ``url`` stripped, or raise if it isn't a plain http(s) URL.

    For scrapers whose slug contract accepts a full careers URL
    (custom-domain tenants). Enforces scheme and the presence of a
    hostname; rejects embedded credentials, which are a classic
    URL-confusion vector.
    """
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ScraperError(
            f"{provider}: invalid careers URL {url!r} — expected an "
            f"http(s) URL like 'https://careers.example.com'."
        )
    if parsed.username or parsed.password:
        raise ScraperError(
            f"{provider}: careers URL {url!r} must not contain credentials."
        )
    return cleaned
