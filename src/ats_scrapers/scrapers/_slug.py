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

import ipaddress
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
    """Return ``url`` stripped, or raise if it isn't a public http(s) URL.

    For scrapers whose slug contract accepts a full careers URL
    (custom-domain tenants). Enforces scheme and the presence of a
    hostname, rejects embedded credentials (a classic URL-confusion
    vector), and refuses targets that statically identify internal
    infrastructure: literal non-public IPs (loopback, RFC-1918,
    link-local incl. 169.254.169.254 cloud metadata, reserved),
    obfuscated numeric hosts, ``localhost``, single-label internal
    hostnames, and ``.internal`` / ``.local`` names.

    DNS-based checks (a public hostname *resolving* to a private IP —
    rebinding) are out of scope for a client library: they'd require
    resolution at validation time and are only meaningfully enforced
    at connect time. Operators feeding untrusted tenant lists into a
    privileged network position should additionally sandbox egress.
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
    reason = _disallowed_host(parsed.hostname.lower())
    if reason:
        raise ScraperError(
            f"{provider}: careers URL {url!r} refused — {reason}. Only "
            f"public hostnames are valid scrape targets."
        )
    return cleaned


def _disallowed_host(host: str) -> str | None:
    """Return a refusal reason for hosts that identify internal targets."""
    bare = host.rstrip(".")
    if bare == "localhost" or bare.endswith(".localhost"):
        return "localhost is not a public host"
    if bare.endswith((".internal", ".local")):
        return f"{bare!r} is an internal-network name"
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        # Not a well-formed IP literal. Hosts made only of digits/dots/
        # colons/hex markers (e.g. "0177.0.0.1", "2130706433") are IP
        # obfuscations that inet_aton-style resolvers still parse.
        if re.fullmatch(r"[0-9xX.:]+", bare):
            return f"{bare!r} looks like an obfuscated IP address"
        if "." not in bare:
            return f"single-label hostname {bare!r} is not a public name"
        return None
    if not ip.is_global:
        return f"IP {bare} is not publicly routable"
    return None
