"""Bumeran / Navent (LATAM family) — multi-country jobboard scraper.

Bumeran is the consumer brand of Navent, a regional jobboard platform
that serves 5+ Spanish-speaking Latin American countries from a single
shared backend. The same React/SPA frontend and same ``/api/avisos/...``
JSON API power every regional site — only the brand, ``x-site-id``
header, and country in the data differ. The sister brands
``zonajobs.com.ar`` (Argentina) and ``multitrabajos.com`` (Ecuador)
also ride on the same backend.

Public JSON API at ``POST /api/avisos/searchV2`` — no auth, no API key.
The request is gated by Cloudflare's bot manager: a bare ``httpx``
request returns 403, but TLS+h2 fingerprint impersonation via
``httpcloak`` (already in the ``scrapers`` extra) plus a one-shot
landing-page GET to acquire the ``__cf_*`` cookies is enough to clear
the WAF for the full session. The same pattern is documented in
``builtin.py`` / ``jazzhr.py``.

API contract reverse-engineered from
``/candidate/static/js/main.<hash>.js`` on the live frontend:

    POST /api/avisos/searchV2?pageSize=<n>&page=<p>&sort=RELEVANTES
    Headers:
      x-site-id: BMAR | BMPE | BMEC | BMVE | ZJAR | ...
      Origin / Referer: matching site
      Content-Type: application/json
    Body: { "filtros": [], "query": "", "internacional": false }
    Response: { number, size, total, content: [aviso, ...], ... }

Each ``aviso`` in ``content`` ships ``id``, ``titulo``, ``detalle``
(plain-text body, ~10kB), ``empresa``, ``localizacion``,
``fechaHoraPublicacion`` (``"DD-MM-YYYY HH:MM:SS"``), ``tipoTrabajo``
(``Full-time``/``Part-time``/...), ``modalidadTrabajo``
(``Presencial``/``Remoto``/``Hibrido``), plus ``idArea`` / ``idSubarea``
classification ids. We map the structured fields to the canonical
``Job`` schema and keep the area / portal / modality on ``raw`` so the
downstream pipeline doesn't lose Navent-specific signal.

``company_slug`` picks the region — one of ``ar``, ``pe``, ``ec``,
``ve``, ``ar-zonajobs``, ``ec-multitrabajos``. The ``cl`` (Chile) and
``pe-konzerta`` aliases are recognised in the region table but the
underlying Trabajando.com / Konzerta stacks don't share this API; they
fall through to an empty result with a logged warning rather than
crashing the publish pipeline.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
from datetime import UTC, datetime
from importlib.util import find_spec
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

# Per-region: base URL, site-id header value, ISO 3166-1 alpha-2 country,
# ISO 4217 currency (best-effort — the API rarely exposes salary).
# Order matters for the public-API docstring but not for behaviour.
REGIONS: dict[str, tuple[str, str, str, str, str]] = {
    # company_slug -> (base_url, x_site_id, country_iso, language, currency)
    "ar": ("https://www.bumeran.com.ar", "BMAR", "AR", "es", "ARS"),
    "pe": ("https://www.bumeran.com.pe", "BMPE", "PE", "es", "PEN"),
    "ec": ("https://www.bumeran.com.ec", "BMEC", "EC", "es", "USD"),
    "ve": ("https://www.bumeran.com.ve", "BMVE", "VE", "es", "VES"),
    "cl": ("https://www.bumeran.cl", "BMCL", "CL", "es", "CLP"),
    "ar-zonajobs": ("https://www.zonajobs.com.ar", "ZJAR", "AR", "es", "ARS"),
    "pe-konzerta": ("https://www.konzerta.com.pe", "BMPE", "PE", "es", "PEN"),
    # multitrabajos shares the BMEC site-id with bumeran.com.ec — same
    # backend tenant, different brand surface.
    "ec-multitrabajos": ("https://www.multitrabajos.com", "BMEC", "EC", "es", "USD"),
}
REGION_TIMEZONES = {
    "ar": "America/Argentina/Buenos_Aires",
    "ar-zonajobs": "America/Argentina/Buenos_Aires",
    "pe": "America/Lima",
    "pe-konzerta": "America/Lima",
    "ec": "America/Guayaquil",
    "ec-multitrabajos": "America/Guayaquil",
    "ve": "America/Caracas",
    "cl": "America/Santiago",
}

# Sites whose backend isn't on the shared searchV2 API. The scraper
# accepts the slug (so the caller can keep a uniform config) but logs a
# warning and returns an empty list. Move out once a working endpoint
# is documented.
_UNSUPPORTED_SLUGS: frozenset[str] = frozenset({"cl", "pe-konzerta"})

PAGE_SIZE = 100  # API hard-caps at 100; lower values just paginate more.
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2.0
# Cloudflare rate-limits aggressive bursts on the same edge cookie.
# Three concurrent page fetches finishes a 100k-row country in ~5min.
MAX_CONCURRENCY = 3

# Per ``tipoTrabajo`` value -> canonical EmploymentType. Lowercased
# before lookup so "Full-time" / "FULL_TIME" / "full-time" all match.
_TIPO_TRABAJO_MAP: dict[str, str] = {
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "tiempo completo": "FULL_TIME",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "medio tiempo": "PART_TIME",
    "tiempo parcial": "PART_TIME",
    "freelance": "CONTRACT",
    "contractor": "CONTRACT",
    "por hora": "CONTRACT",
    "temporario": "TEMPORARY",
    "temporal": "TEMPORARY",
    "pasantia": "INTERN",
    "pasantía": "INTERN",
    "practicas": "INTERN",
    "prácticas": "INTERN",
    "internship": "INTERN",
    "becario": "INTERN",
    "primer empleo": "FULL_TIME",
}

_TAG_RE = re.compile(r"<[^>]+>")
# Bumeran's API ships ``fechaHoraPublicacion`` as ``DD-MM-YYYY HH:MM:SS``
# in the site's local time. We only have date-level granularity for
# matching, so the time part is best-effort.
_DATE_FMT = "%d-%m-%Y %H:%M:%S"
_DATE_ONLY_FMT = "%d-%m-%Y"


@ScraperRegistry.register(ATSType.BUMERAN)
class BumeranScraper(BaseScraper):
    """Bumeran / Navent (LATAM family) — multi-country jobboard.

    ``company_slug`` selects the country / brand. Recognised values:

    - ``ar`` — bumeran.com.ar (Argentina)
    - ``pe`` — bumeran.com.pe (Peru)
    - ``ec`` — bumeran.com.ec (Ecuador)
    - ``ve`` — bumeran.com.ve (Venezuela)
    - ``cl`` — bumeran.cl (Chile) — *not on shared API; returns []*
    - ``ar-zonajobs`` — zonajobs.com.ar (Argentina alt brand)
    - ``pe-konzerta`` — konzerta.com.pe (Peru alt brand) — *unreachable*
    - ``ec-multitrabajos`` — multitrabajos.com (Ecuador alt brand)

    Optional knobs:

    - ``max_pages`` — optional explicit pagination cap for bounded probes;
      production defaults to the full reported catalogue.
    - ``client_kind`` — ``"httpcloak"`` (default) uses TLS+h2
      impersonation to clear Cloudflare; ``"httpx"`` skips httpcloak
      for diagnostic comparison and will 403 in production.
    - ``proxy`` / ``include_descriptions`` — standard scraper options,
      forwarded to the selected transport and parser respectively.

    The class returns ``[]`` (with a warning log) when ``httpcloak``
    isn't installed so the publish pipeline doesn't crash on operators
    that haven't picked up the ``scrapers`` extra.
    """

    ats = ATSType.BUMERAN

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int | None = None,
        include_descriptions: bool = True,
        proxy: str | None = None,
        client_kind: str = "httpcloak",
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if company_slug not in REGIONS:
            raise CompanyNotFoundError(
                f"Bumeran: unknown region {company_slug!r}. "
                f"Known: {sorted(REGIONS)}"
            )
        self.max_pages = max_pages
        if client_kind not in {"httpcloak", "httpx"}:
            raise ValueError("client_kind must be 'httpcloak' or 'httpx'")
        self.client_kind = client_kind
        self._timezone = ZoneInfo(REGION_TIMEZONES[company_slug])
        (
            self._base_url,
            self._site_id,
            self._country_iso,
            self._language,
            self._currency,
        ) = REGIONS[company_slug]

    async def afetch(self) -> list[Job]:
        if self.company_slug in _UNSUPPORTED_SLUGS:
            log.warning(
                "Bumeran: region %r is recognised but its backend is not "
                "on the shared searchV2 API yet — returning [].",
                self.company_slug,
            )
            return []
        if self.client_kind == "httpcloak" and find_spec("httpcloak") is None:
            log.warning(
                "Bumeran: httpcloak is required to clear Cloudflare on "
                "Navent's API — install with `pip install ats_scrapers[scrapers]`. "
                "Returning []."
            )
            return []
        return await self._fetch_async()

    def fetch(self) -> list[Job]:
        return self._run_sync(self.afetch())

    # --- listing fetch ------------------------------------------------------

    async def _fetch_async(self) -> list[Job]:
        # Establish a single httpcloak session for the whole scrape. The
        # landing-page GET seeds Cloudflare's __cf_* cookies that the
        # subsequent /api/avisos/searchV2 POSTs need to pass the WAF.
        session = await asyncio.to_thread(self._open_session)
        first = await asyncio.to_thread(self._search_page, session, 0)
        total = int(first.get("total") or 0)
        size = int(first.get("size") or PAGE_SIZE)
        if size <= 0:
            size = PAGE_SIZE
        # ``size`` from the API is the number of rows actually returned
        # on this page, not the requested pageSize — when only a handful
        # of jobs exist, ``size`` is small but the math still holds.
        total_pages = (total + size - 1) // size if total else 1
        if self.max_pages is not None:
            total_pages = min(self.max_pages, total_pages)

        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(payload: dict[str, Any]) -> None:
            parsed = [self._parse_job(item) for item in (payload.get("content") or [])]
            async with lock:
                for j in parsed:
                    if j is None or j.ats_id in seen:
                        continue
                    seen.add(j.ats_id)
                    jobs.append(j)

        await absorb(first)
        if total_pages <= 1:
            return jobs

        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def per_page(page: int) -> None:
            async with sem:
                payload = await asyncio.to_thread(
                    self._search_page, session, page,
                )
            await absorb(payload)

        await asyncio.gather(*(per_page(p) for p in range(1, total_pages)))
        return jobs

    # --- httpcloak transport ------------------------------------------------

    def _open_session(self) -> Any:
        """Create an httpcloak Session, prime it with the landing page so
        Cloudflare drops the right cookies, and return it."""
        if self.client_kind == "httpx":
            session: Any = httpx.Client(
                proxy=self.proxy,
                follow_redirects=True,
            )
        else:
            import httpcloak

            session = (
                httpcloak.Session(proxy=self.proxy)
                if self.proxy
                else httpcloak.Session()
            )
        landing = f"{self._base_url}/empleos.html"
        try:
            response = session.get(landing, timeout=self.timeout)
        except Exception as exc:
            raise ScraperError(
                f"Bumeran ({self.company_slug}): landing-page fetch "
                f"failed: {exc}"
            ) from exc
        if response.status_code != 200:
            raise ScraperError(
                f"Bumeran ({self.company_slug}): landing returned "
                f"{response.status_code} (expected 200)"
            )
        return session

    def _search_page(self, session: Any, page: int) -> dict[str, Any]:
        url = (
            f"{self._base_url}/api/avisos/searchV2"
            f"?pageSize={PAGE_SIZE}&page={page}&sort=RELEVANTES"
        )
        body = {"filtros": [], "query": "", "internacional": False}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/empleos.html",
            "x-site-id": self._site_id,
        }

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = session.post(
                    url, headers=headers, json=body, timeout=self.timeout,
                )
            except Exception as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Bumeran ({self.company_slug}) page {page}: "
                        f"transport failed: {exc}"
                    ) from exc
                _sleep(RETRY_BASE_DELAY * attempt)
                continue
            status = int(getattr(response, "status_code", 0))
            text = _response_text(response)
            if status == 200:
                try:
                    payload = json.loads(text)
                except ValueError as exc:
                    # Cloudflare sometimes returns 200 + HTML challenge
                    # body on transient flaky edges — retry rather than
                    # surface a misleading parse error.
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Bumeran ({self.company_slug}) page {page}: "
                            f"non-JSON 200 body: {exc}"
                        ) from exc
                    _sleep(RETRY_BASE_DELAY * attempt)
                    continue
                if not isinstance(payload, dict):
                    raise ScraperError(
                        f"Bumeran ({self.company_slug}) page {page}: "
                        f"expected object, got {type(payload).__name__}"
                    )
                return payload
            if status == 404:
                # Past the last page — caller treats empty content as EOF.
                return {"content": [], "total": 0, "size": 0, "number": page}
            if status == 403 or status == 429 or 500 <= status < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Bumeran ({self.company_slug}) page {page}: "
                        f"status {status} after {MAX_RETRIES} retries"
                    )
                _sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            raise ScraperError(
                f"Bumeran ({self.company_slug}) page {page}: "
                f"unexpected status {status}"
            )
        raise ScraperError(
            f"Bumeran ({self.company_slug}) page {page}: "
            f"exhausted retries: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        if raw_id is None:
            return None
        ats_id = str(raw_id)
        title = (item.get("titulo") or "").strip()
        if not ats_id or not title:
            return None

        # Synthesize the canonical detail URL — Bumeran's frontend uses
        # ``/empleos/<slug>-<id>.html`` and accepts ``aviso-<id>.html``
        # as an alias (verified live: 200 with the SPA shell, which
        # then bootstraps the same detail data we already have from the
        # listing). Use the alias so we don't have to slugify titles
        # with diacritics.
        url = f"{self._base_url}/empleos/aviso-{ats_id}.html"

        company_name = (item.get("empresa") or "").strip()
        # Bumeran flags confidential listings — keep the empty company
        # value visible as "Confidencial" so downstream rows don't
        # silently fall back to a numeric employer id.
        if item.get("confidencial") and not company_name:
            company_name = "Confidencial"
        if not company_name:
            company_name = "Unknown"

        location = (item.get("localizacion") or "").strip() or None

        modalidad = (item.get("modalidadTrabajo") or "").strip()
        is_remote: bool | None = None
        if modalidad:
            mlow = modalidad.lower()
            if "remoto" in mlow or "remote" in mlow or "home office" in mlow:
                is_remote = True
            elif "presencial" in mlow:
                is_remote = False
            # Hybrid / mixed leaves is_remote=None — the downstream
            # enrichment can decide once it sees the description.

        tipo = (item.get("tipoTrabajo") or "").strip()
        employment_type = _TIPO_TRABAJO_MAP.get(tipo.lower())

        description = (
            _clean_description(item.get("detalle"))
            if self.include_descriptions
            else None
        )

        posted_at = _parse_datetime(
            item.get("fechaHoraPublicacion") or item.get("fechaPublicacion"),
            timezone=self._timezone,
        )

        raw: dict[str, Any] = {}
        for key in (
            "idArea", "idSubarea", "idEmpresa", "idPais",
            "portal", "planPublicacion", "modalidadTrabajo",
            "tipoTrabajo", "tipoAviso", "cantidadVacantes",
            "aptoDiscapacitado", "confidencial", "empresaPro",
            "postulacionRapida",
        ):
            v = item.get(key)
            if v not in (None, "", []):
                raw[key] = v
        raw["country_alias"] = self.company_slug
        raw["site_id"] = self._site_id

        return Job(
            url=url,
            title=title,
            company=company_name,
            ats_type=ATSType.BUMERAN,
            ats_id=ats_id,
            location=location,
            country_iso=self._country_iso,
            region="South America",
            is_remote=is_remote,
            employment_type=employment_type,
            commitment=tipo or None,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            language=self._language,
            raw=raw or None,
        )


# --- helpers ----------------------------------------------------------------


def _response_text(response: Any) -> str:
    """Return response body as text, tolerating httpcloak's variable API."""
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content)


def _sleep(seconds: float) -> None:
    """Synchronous sleep used inside the httpcloak (blocking) path. We
    can't call ``asyncio.sleep`` here because the function runs inside
    ``asyncio.to_thread`` so the event loop is on another thread."""
    import time

    time.sleep(seconds)


def _clean_description(value: object) -> str | None:
    """Bumeran's ``detalle`` ships as plain text but with HTML entities
    (``&#x1f50e;``, ``&amp;``) interleaved. Decode entities, strip any
    stray tags, collapse whitespace, and truncate to the canonical
    ~10kB description budget."""
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = _TAG_RE.sub(" ", value)
    cleaned = _html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:10_000]


def _parse_datetime(
    value: object,
    *,
    timezone: ZoneInfo,
) -> datetime | None:
    """Parse Bumeran's ``DD-MM-YYYY HH:MM:SS`` (and date-only fallback).

    The API ships local time without a zone, so interpret it in the
    selected regional timezone and normalize to UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    for fmt in (_DATE_FMT, _DATE_ONLY_FMT):
        try:
            return datetime.strptime(s, fmt).replace(
                tzinfo=timezone,
            ).astimezone(UTC)
        except ValueError:
            continue
    return None
