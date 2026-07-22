"""Tenant-slug validation tests (GH-181 SSRF fix).

Scrapers that interpolate ``company_slug`` into a URL *hostname* must
reject slugs that would change the request host (dots, slashes, query
strings, credentials, non-http schemes). Validation happens at
construction time via :mod:`ats_scrapers.scrapers._slug`.
"""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.scrapers import (
    AvatureScraper,
    BambooHRScraper,
    BreezyScraper,
    CornerstoneScraper,
    EightfoldScraper,
    JazzHRScraper,
    PersonioScraper,
    PinpointScraper,
    RecruiteeScraper,
    TeamtailorScraper,
    iCIMSScraper,
)
from ats_scrapers.scrapers._slug import require_host_label, require_http_url

BARE_SLUG_SCRAPERS = [
    TeamtailorScraper,
    BreezyScraper,
    BambooHRScraper,
    PinpointScraper,
    JazzHRScraper,
    EightfoldScraper,
]
SLUG_OR_URL_SCRAPERS = [
    RecruiteeScraper,
    PersonioScraper,
    AvatureScraper,
    iCIMSScraper,
    CornerstoneScraper,
]
ALL_PATCHED_SCRAPERS = BARE_SLUG_SCRAPERS + SLUG_OR_URL_SCRAPERS


# --- require_host_label ------------------------------------------------------


@pytest.mark.parametrize("slug", ["acme", "ACME", "a", "10-4-truck-recruiting", "1komma5"])
def test_host_label_accepts_valid_slugs(slug: str) -> None:
    assert require_host_label(slug, provider="X") == slug


def test_host_label_strips_whitespace() -> None:
    assert require_host_label("  acme  ", provider="X") == "acme"


@pytest.mark.parametrize(
    "slug",
    [
        "evil.com",  # dot escapes the parent domain
        "evil.com/x?y=",  # dot + path + query
        "a/b",  # slash
        "a?b",  # query
        "",  # empty
        "   ",  # whitespace only
        "-leading",  # hyphen at edge
        "trailing-",
        "a" * 64,  # longer than one DNS label
        "user:pass@host",
        "a b",  # inner whitespace
    ],
)
def test_host_label_rejects_bad_slugs(slug: str) -> None:
    with pytest.raises(ScraperError):
        require_host_label(slug, provider="X")


def test_host_label_error_names_provider() -> None:
    with pytest.raises(ScraperError, match="RecruiteeScraper"):
        require_host_label("evil.com", provider="RecruiteeScraper")


# --- require_http_url --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example.com",
        "http://careers.example.com",
        "https://careers.example.com/en_US/SearchJobs",
    ],
)
def test_http_url_accepts_plain_http_urls(url: str) -> None:
    assert require_http_url(url, provider="X") == url


def test_http_url_strips_whitespace() -> None:
    assert (
        require_http_url("  https://a.example.com ", provider="X")
        == "https://a.example.com"
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # non-http scheme
        "ftp://example.com",
        "https://",  # no hostname
        "https://user:pass@host",  # embedded credentials
        "https://user@host",  # username only
        "",  # empty
        "evil.com",  # no scheme
    ],
)
def test_http_url_rejects_bad_urls(url: str) -> None:
    with pytest.raises(ScraperError):
        require_http_url(url, provider="X")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",          # loopback
        "http://[::1]/",                        # IPv6 loopback
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",                     # RFC-1918
        "http://172.16.3.4/",                   # RFC-1918
        "http://192.168.1.1/",                  # RFC-1918
        "http://0.0.0.0/",                      # unspecified
        "http://localhost/x",                   # localhost by name
        "http://foo.localhost/",                # .localhost suffix
        "http://metadata.google.internal/",     # .internal name
        "http://printer.local/",                # .local name
        "http://intranet/",                     # single-label host
        "http://0177.0.0.1/",                   # octal-obfuscated 127.0.0.1
        "http://2130706433/",                   # decimal-obfuscated 127.0.0.1
    ],
)
def test_http_url_rejects_internal_targets(url: str) -> None:
    """GH-181's explicit ask: private ranges and metadata endpoints
    must not be reachable through the full-URL slug contract."""
    with pytest.raises(ScraperError, match="refused"):
        require_http_url(url, provider="X")


def test_http_url_accepts_public_ip_literal() -> None:
    assert require_http_url("https://8.8.8.8/jobs", provider="X")


# --- construction-time enforcement per scraper -------------------------------


@pytest.mark.parametrize("scraper_cls", ALL_PATCHED_SCRAPERS)
def test_hostile_slug_rejected_at_construction(scraper_cls: type) -> None:
    with pytest.raises(ScraperError):
        scraper_cls("evil.com/x?y=")


@pytest.mark.parametrize("scraper_cls", SLUG_OR_URL_SCRAPERS)
def test_file_scheme_rejected(scraper_cls: type) -> None:
    with pytest.raises(ScraperError):
        scraper_cls("file:///etc/passwd")


@pytest.mark.parametrize("scraper_cls", SLUG_OR_URL_SCRAPERS)
def test_credentialed_url_rejected(scraper_cls: type) -> None:
    with pytest.raises(ScraperError):
        scraper_cls("https://user:pass@host")


@pytest.mark.parametrize("scraper_cls", ALL_PATCHED_SCRAPERS)
def test_normal_slug_constructs(scraper_cls: type) -> None:
    scraper = scraper_cls("acme")
    assert scraper.company_slug == "acme"


@pytest.mark.parametrize("scraper_cls", SLUG_OR_URL_SCRAPERS)
def test_full_url_slug_constructs(scraper_cls: type) -> None:
    scraper = scraper_cls("https://careers.example.com")
    assert scraper.company_slug == "https://careers.example.com"


def test_eightfold_base_url_validated() -> None:
    with pytest.raises(ScraperError):
        EightfoldScraper("acme", base_url="file:///etc/passwd")
    with pytest.raises(ScraperError):
        EightfoldScraper("acme", base_url="https://user:pass@host")


def test_eightfold_valid_base_url_constructs() -> None:
    s = EightfoldScraper("acme", base_url="https://apply.careers.example.com")
    assert s.base_url == "https://apply.careers.example.com"
