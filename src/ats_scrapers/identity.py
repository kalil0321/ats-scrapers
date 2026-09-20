"""Posting identity shared by scrapers, dataset exports and the client."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlparse
from uuid import uuid4

from pydantic import HttpUrl, ValidationError

URL_SCOPED_PROVIDERS = frozenset({"greenhouse", "lever", "ashby", "recruitee"})
INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f]")
log = logging.getLogger(__name__)


def canonical_job_url(url: str, provider: str) -> str:
    """Normalize known presentation variants, preserving tenant and job selectors.

    Invalid or absent URLs cannot safely namespace an identifier and raise
    ValueError. Custom-domain aliases are deliberately not guessed.
    """
    try:
        parsed = urlparse(str(HttpUrl(url)))
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except (ValidationError, ValueError) as exc:
        raise ValueError("A valid HTTP posting URL is required for identity") from exc
    if ":" in host:
        host = f"[{host}]"
    default_port = {"http": 80, "https": 443}.get(parsed.scheme)
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    if provider == "recruitee" and re.fullmatch(r"/o/[^/]+/apply", path):
        path = path.removesuffix("/apply")
    greenhouse_job = (
        re.fullmatch(r"/[^/]+/jobs/(\d+)", path)
        if provider == "greenhouse" and host in {
            "boards.greenhouse.io", "job-boards.greenhouse.io",
            "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
        } else None
    )
    query = urlencode(sorted((
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and not (provider == "greenhouse" and key.casefold() == "gh_src")
        and not (provider == "lever" and key.casefold() in {"lever-source", "lever-origin"})
        and not (key == "gh_jid" and greenhouse_job and value == greenhouse_job[1])
    ), key=lambda item: item[0]))
    return f"{host}{path}" + (f"?{query}" if query else "")


def build_global_id(ats_type: str, ats_id: str | None, url: str = "") -> str:
    """Derive an opaque posting ID without depending on an employer display name.

    The four expanded catalog providers use v2 URL-scoped identities. Other
    providers retain their existing composite IDs. Missing identity inputs use
    the historical random UUID fallback, never an unscoped v2 identifier.
    """
    provider = ats_type.strip()
    native_id = (ats_id or "").strip()
    if not provider or INVALID_ID_CHARS.search(provider) or not native_id or INVALID_ID_CHARS.search(native_id):
        return str(uuid4())
    if provider not in URL_SCOPED_PROVIDERS:
        return f"{provider}:{native_id}"
    try:
        canonical_url = canonical_job_url(url, provider)
    except ValueError:
        log.error("Cannot namespace %s posting without a valid URL; using UUID fallback", provider)
        return str(uuid4())
    payload = json.dumps([canonical_url, native_id], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{provider}:v2:{digest}"
