"""WorkNet (work24.go.kr / work.go.kr) — Korean Ministry of Employment
job board scraper.

WorkNet is operated by the Korean Ministry of Employment & Labor and is
the country's official public-sector job listings service. It exposes a
documented open API ("채용정보 API") via Korea's data portal
(data.go.kr) at:

    GET https://openapi.work.go.kr/opi/opi/opia/wantedApi.do
        ?authKey={WORKNET_API_KEY}
        &callTp=L              # L = list, D = detail
        &returnType=XML        # only XML works; JSON triggers msgCd=004
        &startPage={1..N}
        &display={1..100}

Authentication is by free API key. Apply at https://www.data.go.kr/ or
the WorkNet open-API portal, then store it as ``WORKNET_API_KEY``. When
the env var is missing, :meth:`fetch` raises :class:`ScraperError` with
a clear pointer to the docs — the scraper gracefully degrades rather
than crashing the cron with an opaque traceback.

Typical run: 100-400k active postings spread across public-sector
employers, mid-size Korean SMEs that publish through the government
service, and rotating regional-government job programmes. The
single-source pattern follows Bundesagentur / USAJOBS: ``company_slug``
is informational (used for logging only) and we always pull the full
active dataset.

Field mapping (XML element → Job field):

* ``wantedAuthNo`` → ``ats_id`` (composite ID of the form
  ``K1622...``, stable per posting).
* ``empWantedTitle`` → ``title``
* ``coNm`` → ``company`` (employer name).
* ``workRegion`` → ``location``.
* ``empWantedHomepgDetail`` / detail URL → ``url``.
* ``regDt``, ``regDtEnd`` → ``posted_at`` and ``raw.expires_at``.
* ``salTpNm`` / ``sal`` → ``salary_summary`` (free text; structured
  min/max derivation happens downstream).
* ``empWantedTypeNm`` → ``commitment`` (raw Korean label).
* ``jobsCd`` / ``industry`` / ``career`` / ``salTpCd`` → ``raw`` dict.

``country_iso="KR"`` and ``language="ko"`` are set unconditionally —
WorkNet is single-country, Korean-only by definition.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://openapi.work.go.kr/opi/opi/opia/wantedApi.do"
DETAIL_URL_TEMPLATE = (
    "https://www.work24.go.kr/wk/a/b/1500/empDetailAuthView.do?wantedAuthNo={ats_id}"
)
DEFAULT_PAGE_SIZE = 100  # API hard-caps at 100 (display=100).
DEFAULT_MAX_PAGES = 200  # 20k postings/run safety bound; bump for full crawls.
MAX_CONCURRENCY = 2  # Korean gov APIs throttle aggressively above this.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
ENV_API_KEY = "WORKNET_API_KEY"

# API responses describe terminal application errors via ``<messageCd>``.
# 000 / 200 indicate success; any other value is a contract break
# (invalid key, malformed params, etc.) and we surface it as a hard
# ``ScraperError``. The text label is in Korean.
_APP_ERROR_CODES = {
    "002": "Invalid authentication key",
    "003": "Daily quota exceeded",
    "004": "Invalid returnType (only XML is supported)",
    "018": "Missing required parameter for detail view",
}

# WorkNet's Korean employment-type labels → canonical EmploymentType.
# The API exposes ``empWantedTypeNm`` as a free-text Korean label; the
# code-form ``empWantedTypeCd`` is taxonomy-stable but Korean labels
# carry more nuance, so we map both.
_TYPE_LABEL_MAP = {
    "정규직": "FULL_TIME",
    "계약직": "CONTRACT",
    "기간제": "CONTRACT",
    "파견직": "TEMPORARY",
    "일용직": "TEMPORARY",
    "인턴": "INTERN",
    "아르바이트": "PART_TIME",
}

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MAX_DESCRIPTION_LEN = 10_000


@ScraperRegistry.register(ATSType.WORKNETKR)
class WorkNetKoreaScraper(BaseScraper):
    """WorkNet (Korea, work24.go.kr) scraper. Single-source —
    ``company_slug`` is unused.

    Reads ``WORKNET_API_KEY`` from the environment; raises
    :class:`ScraperError` when missing. Free API keys are issued at
    https://www.data.go.kr/ or the WorkNet Open-API portal.
    """

    ats = ATSType.WORKNETKR

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.page_size = page_size
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        api_key = os.environ.get(ENV_API_KEY, "").strip()
        if not api_key:
            raise ScraperError(
                f"{ENV_API_KEY} env var is required. Register at "
                "https://www.data.go.kr/ (search '워크넷 채용정보') or "
                "https://www.work24.go.kr/cm/e/a/0110/selectOpenApiIntro.do "
                "for a free key."
            )
        return asyncio.run(self._fetch_async(api_key))

    async def _fetch_async(self, api_key: str) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            page = 1
            while page <= self.max_pages:
                items = await self._fetch_page(client, sem, api_key, page)
                if not items:
                    break
                new_count = 0
                for item in items:
                    job = self._parse_item(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    new_count += 1
                # The API returns fewer than display=page_size when the
                # last page is reached — that's the termination signal.
                if len(items) < self.page_size or new_count == 0:
                    break
                page += 1
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        api_key: str,
        page: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "authKey": api_key,
            "callTp": "L",
            "returnType": "XML",
            "startPage": page,
            "display": self.page_size,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL,
                        params=params,
                        headers={
                            "User-Agent": "Mozilla/5.0 (stapply-ai jobhive)",
                            "Accept": "application/xml,text/xml,*/*",
                            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"WorkNet fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return self._parse_list_xml(response.text, page=page)
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"WorkNet returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2**attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"WorkNet returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"WorkNet exhausted retries at page={page}: {last_exc}"
        )

    def _parse_list_xml(self, body: str, *, page: int) -> list[dict[str, Any]]:
        """Parse the ``<wantedRoot>`` XML list response into row dicts.

        Surfaces ``<messageCd>`` errors as :class:`ScraperError` — those
        are contract breaks (bad key, quota exhausted) and must not be
        swallowed as ``[]``. Empty result sets come back as a
        well-formed root with zero ``<wanted>`` children, which is
        different from an error response and returns ``[]`` cleanly.
        """
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ScraperError(
                f"WorkNet returned malformed XML at page={page}: {exc}"
            ) from exc

        message_cd = root.findtext("messageCd")
        if message_cd and message_cd not in ("", "000", "200"):
            label = _APP_ERROR_CODES.get(
                message_cd, root.findtext("message") or "unknown error"
            )
            raise ScraperError(
                f"WorkNet API error msgCd={message_cd} at page={page}: {label}"
            )

        items: list[dict[str, Any]] = []
        # The API wraps each posting in ``<wanted>`` under
        # ``<wantedRoot>`` — there are no namespaces or attributes to
        # juggle, just direct child text.
        for posting in root.findall("wanted"):
            row: dict[str, Any] = {}
            for child in posting:
                if child.text is None:
                    continue
                value = child.text.strip()
                if value:
                    row[child.tag] = value
            if row:
                items.append(row)
        return items

    def _parse_item(self, item: dict[str, Any]) -> Job | None:
        ats_id = (item.get("wantedAuthNo") or "").strip()
        title = (item.get("empWantedTitle") or item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        company = (item.get("coNm") or "WorkNet").strip() or "WorkNet"

        # Prefer the API-provided detail URL when present; fall back to
        # the canonical work24 detail page built from wantedAuthNo.
        url_field = (
            item.get("empWantedHomepgDetail")
            or item.get("empWantedHomepg")
            or item.get("empWantedInfoUrl")
        )
        if isinstance(url_field, str) and url_field.startswith("http"):
            url = url_field.strip()
        else:
            url = DETAIL_URL_TEMPLATE.format(ats_id=ats_id)

        location = _first_nonempty(
            item.get("workRegion"),
            item.get("workPlcNm"),
            item.get("region"),
        )

        type_label = (item.get("empWantedTypeNm") or "").strip() or None
        employment_type = _TYPE_LABEL_MAP.get(type_label) if type_label else None

        salary_summary = _first_nonempty(
            item.get("sal"),
            item.get("salTpNm"),
        )
        # Only attach a currency when there is a salary signal to
        # describe — empty salary_summary + KRW currency would lie about
        # the row having compensation data.
        salary_currency = "KRW" if salary_summary else None

        posted_at = _parse_date(item.get("regDt") or item.get("regDate"))

        # Free-text job description varies across endpoints; the list
        # call doesn't return the full HTML body, but it does include a
        # short "preview" / required-condition field on most rows.
        description = _html_to_text(
            item.get("empWantedContents")
            or item.get("empBassRequdCont")
            or item.get("jobCont")
        )

        raw: dict[str, Any] = {}
        for source, dest in (
            ("jobsCd", "job_type_code"),
            ("salTpCd", "wage_code"),
            ("indCd", "industry_code"),
            ("career", "career_level"),
            ("careerCd", "career_code"),
            ("empWantedTypeCd", "employment_type_code"),
            ("regDtEnd", "expires_at"),
            ("eduNm", "education_label"),
            ("eduCd", "education_code"),
            ("collectPsncnt", "headcount"),
        ):
            value = item.get(source)
            if value not in (None, ""):
                raw[dest] = value

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.WORKNETKR,
            ats_id=ats_id,
            location=location,
            country_iso="KR",
            language="ko",
            employment_type=employment_type,
            commitment=type_label,
            description=description,
            salary_summary=salary_summary,
            salary_currency=salary_currency,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _first_nonempty(*values: object) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = HTML_TAG_RE.sub(" ", value)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:MAX_DESCRIPTION_LEN]


def _parse_date(value: object) -> datetime | None:
    """Parse WorkNet date strings.

    The list endpoint emits ``YYYYMMDD`` (eight-digit, no separators) or
    occasionally ``YYYY-MM-DD``. Both are surface-formats over the same
    underlying date — try both before giving up.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None
