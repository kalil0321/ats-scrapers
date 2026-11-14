"""Feishu Hire (飞书招聘) career sites.

An employer on Feishu Hire publishes under ``https://{tenant}.jobs.feishu.cn``.
Each tenant runs one or more career sites, addressed by the first path
segment (``index``, ``campus``, a number, ...), and every site has its own
list of postings::

    POST /api/v1/csrf/token          -> {"data": {"token": ...}}
    POST /api/v1/search/job/posts    -> {"data": {"count": n, "job_post_list": [...]}}

Both calls carry the site name in the ``website-path`` header. The same
posting can be listed on several sites of a tenant; it is kept once.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import HttpUrl

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._throne import (
    compose_description,
    extract_label,
    extract_location,
    map_recruit_type,
)
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

HOST_SUFFIX = ".jobs.feishu.cn"
TOKEN_PATH = "/api/v1/csrf/token"
SEARCH_PATH = "/api/v1/search/job/posts"
DEFAULT_SITES = ("index",)
PAGE_SIZE = 100
SITE_NOT_FOUND_CODE = -9000003

_TENANT_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_SITE_RE = re.compile(r"[A-Za-z0-9_-]+")


@ScraperRegistry.register(ATSType.FEISHU_HIRE)
class FeishuHireScraper(BaseScraper):
    """One Feishu Hire tenant.

    ``company_slug`` is the tenant label (``nio``), its host
    (``nio.jobs.feishu.cn``) or a site URL (``https://nio.jobs.feishu.cn/campus``).
    ``sites`` lists the career sites to read, as a sequence or a
    ``;``-separated string; a site URL contributes its own site.
    """

    ats = ATSType.FEISHU_HIRE

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        sites: Iterable[str] | str | None = None,
        company_name: str | None = None,
    ) -> None:
        tenant, url_site = _split_slug(company_slug)
        super().__init__(
            tenant,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.base_url = f"https://{tenant}{HOST_SUFFIX}"
        self.sites = _site_names(sites, url_site)
        self.company_name = (company_name or "").strip() or tenant

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}
        found_a_site = False
        async with self.make_fetcher() as fetch:
            for site in self.sites:
                postings = await self._site_postings(fetch, site)
                if postings is None:
                    logger.warning(
                        "Feishu Hire (%s) has no site %r", self.company_slug, site
                    )
                    continue
                found_a_site = True
                for item in postings:
                    job = self._parse_job(item, site)
                    if job is not None and job.ats_id not in jobs:
                        jobs[job.ats_id] = job  # type: ignore[index]
        if not found_a_site:
            raise CompanyNotFoundError(
                f"Feishu Hire tenant {self.company_slug!r} has none of the "
                f"sites {', '.join(self.sites)}"
            )
        return list(jobs.values())

    async def _site_postings(
        self, fetch: Fetcher, site: str
    ) -> list[dict[str, Any]] | None:
        """Every posting of one site, or ``None`` when the site does not exist."""
        headers = {
            "content-type": "application/json",
            "website-path": site,
            "portal-channel": "saas-career",
            "portal-platform": "pc",
            "origin": self.base_url,
            "referer": f"{self.base_url}/{site}",
        }
        token_body = await fetch.post_json(
            self.base_url + TOKEN_PATH, headers=headers, json={"portal_entrance": 1}
        )
        token = _token(token_body)
        if token:
            headers["x-csrf-token"] = token

        postings: list[dict[str, Any]] = []
        offset = 0
        while True:
            body = await fetch.post_json(
                self.base_url + SEARCH_PATH,
                headers=headers,
                json=_search_body(offset),
            )
            if not isinstance(body, dict):
                raise ScraperError(
                    f"Feishu Hire ({self.company_slug}/{site}) returned a "
                    "non-object response"
                )
            code = body.get("code")
            if code == SITE_NOT_FOUND_CODE:
                return None
            if code:
                raise ScraperError(
                    f"Feishu Hire ({self.company_slug}/{site}) error {code}: "
                    f"{body.get('message')}"
                )
            data = body.get("data")
            page = data.get("job_post_list") if isinstance(data, dict) else None
            if not isinstance(page, list):
                raise ScraperError(
                    f"Feishu Hire ({self.company_slug}/{site}) omitted job_post_list"
                )
            postings.extend(item for item in page if isinstance(item, dict))
            offset += len(page)
            total = data.get("count")
            if not page or not isinstance(total, int) or offset >= total:
                return postings

    def _parse_job(self, item: dict[str, Any], site: str) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not ats_id or not title:
            return None
        employment_type, commitment = map_recruit_type(item.get("recruit_type"))
        department = extract_label(item.get("job_function")) or extract_label(
            item.get("job_category")
        )
        team = _subject_label(item.get("job_subject"))
        code = item.get("code")
        raw: dict[str, Any] = {"site": site}
        recruit_parent = (item.get("recruit_type") or {}).get("parent")
        if label := extract_label(recruit_parent):
            raw["recruit_group"] = label
        return Job(
            url=HttpUrl(f"{self.base_url}/{site}/position/{ats_id}/detail"),
            title=title,
            company=self.company_name,
            ats_type=ATSType.FEISHU_HIRE,
            ats_id=ats_id,
            location=extract_location(item),
            department=department,
            team=team if team and team != department else None,
            employment_type=employment_type,
            commitment=commitment,
            description=(
                compose_description(item.get("description"), item.get("requirement"))
                if self.include_descriptions
                else None
            ),
            requisition_id=(
                code.strip() if isinstance(code, str) and code.strip() else None
            ),
            posted_at=_parse_millis(item.get("publish_time")),
            fetched_at=datetime.now(tz=UTC),
            raw=raw,
        )


def _split_slug(company_slug: str) -> tuple[str, str | None]:
    """``(tenant, site named by a URL slug or None)``."""
    slug = company_slug.strip()
    site: str | None = None
    if "://" in slug:
        parsed = urlparse(slug)
        host = (parsed.hostname or "").lower()
        segments = [part for part in parsed.path.split("/") if part]
        site = segments[0] if segments else None
    else:
        host = slug.lower().rstrip("/")
    tenant = host.removesuffix(HOST_SUFFIX)
    if not _TENANT_RE.fullmatch(tenant):
        raise ScraperError(
            f"FeishuHireScraper needs a tenant of *{HOST_SUFFIX}, got {company_slug!r}"
        )
    return tenant, site


def _site_names(sites: Iterable[str] | str | None, url_site: str | None) -> tuple[str, ...]:
    if isinstance(sites, str):
        sites = sites.split(";")
    names = [name.strip() for name in (sites or ()) if name and name.strip()]
    if url_site:
        names.insert(0, url_site)
    names = list(dict.fromkeys(names)) or list(DEFAULT_SITES)
    for name in names:
        if not _SITE_RE.fullmatch(name):
            raise ScraperError(f"FeishuHireScraper got an unsafe site name {name!r}")
    return tuple(names)


def _token(body: object) -> str | None:
    data = body.get("data") if isinstance(body, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def _search_body(offset: int) -> dict[str, Any]:
    return {
        "keyword": "",
        "limit": PAGE_SIZE,
        "offset": offset,
        "job_category_id_list": [],
        "tag_id_list": [],
        "location_code_list": [],
        "subject_id_list": [],
        "recruitment_id_list": [],
        "job_function_id_list": [],
        "portal_entrance": 1,
    }


def _subject_label(value: object) -> str | None:
    """``job_subject.name`` is a dict of translations here, not a string."""
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if isinstance(name, dict):
        for key in ("en_us", "i18n", "zh_cn"):
            label = name.get(key)
            if isinstance(label, str) and label.strip():
                return label.strip()
        return None
    return extract_label(value)


def _parse_millis(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None
