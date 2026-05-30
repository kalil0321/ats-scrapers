"""Microsoft careers scraper.

Microsoft retired ``jobs.careers.microsoft.com``'s in-house gcsservices
API and now redirects every careers entrypoint to
``apply.careers.microsoft.com/careers`` — an Eightfold AI ("PCSX") SPA.
The public-facing host ``jobs.careers.microsoft.com/global/en/search``
serves a 301 to that Eightfold tenant; the listing data lives behind
the same generic PCSX search endpoint every other Eightfold tenant
exposes:

    GET https://apply.careers.microsoft.com/api/pcsx/search
        ?domain=microsoft.com&query=&location=&start=N&sort_by=timestamp

We piggyback on :class:`EightfoldScraper` for the fetch + pagination
mechanics (count-driven concurrent fan-out, retry/backoff on 429/5xx,
optional ``httpcloak`` fallback when a tenant sits behind Akamai) and
re-tag the resulting rows with ``ats_type=ATSType.MICROSOFT`` so the
public dataset gets a first-class Microsoft slot rather than burying
~1.6k Microsoft jobs inside the generic ``eightfold`` partition.

The user-visible job URL is rewritten to the canonical
``jobs.careers.microsoft.com`` host (the SPA front-end), which is what
Microsoft links from press releases, recruiter emails, and SERP
results. The API host (``apply.careers.microsoft.com``) is an
implementation detail.

Operationally: direct ``curl`` to the historical
``gcsservices.careers.microsoft.com`` endpoint now 404s from any IP
(the domain is parked on an Azure error page). Reverse-engineering
the live SPA confirmed the migration to PCSX.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry
from jobhive.scrapers.eightfold import EightfoldScraper

ClientKind = Literal["auto", "httpx", "httpcloak"]

# Tenant constants. Pulled out as module-level so tests can import +
# pin them, and so the legacy gcsservices URL stays grep-able in case
# Microsoft ever flips back.
API_BASE_URL = "https://apply.careers.microsoft.com"
PUBLIC_JOB_HOST = "https://jobs.careers.microsoft.com"
EIGHTFOLD_DOMAIN = "microsoft.com"
COMPANY_NAME = "Microsoft"


@ScraperRegistry.register(ATSType.MICROSOFT)
class MicrosoftScraper(BaseScraper):
    """Microsoft scraper. Single tenant — ``company_slug`` is ignored.

    Internally delegates to :class:`EightfoldScraper` configured for the
    Microsoft custom-domain PCSX tenant, then re-tags every emitted
    :class:`~jobhive.models.Job` with ``ats_type=ATSType.MICROSOFT`` so
    rows land in the Microsoft partition of the published dataset.
    """

    ats: ClassVar[ATSType] = ATSType.MICROSOFT

    def __init__(
        self,
        company_slug: str = "microsoft",
        *,
        timeout: float = 30.0,
        client_kind: ClientKind = "auto",
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        # The underlying Eightfold scraper does the actual work. We keep
        # it as an attribute (rather than subclassing) so this class's
        # registry entry stays cleanly bound to ATSType.MICROSOFT — a
        # subclass would inherit ATSType.EIGHTFOLD via the class
        # attribute and the registration decorator on the parent.
        self._inner = EightfoldScraper(
            company_slug,
            timeout=timeout,
            base_url=API_BASE_URL,
            domain=EIGHTFOLD_DOMAIN,
            company_name=COMPANY_NAME,
            job_url_host=PUBLIC_JOB_HOST,
            client_kind=client_kind,
        )

    def fetch(self) -> list[Job]:
        jobs = self._inner.fetch()
        # Re-tag each row: the inner scraper hard-codes
        # ats_type=ATSType.EIGHTFOLD via its ``ats`` class attribute, but
        # this scraper exists precisely to surface Microsoft as a
        # first-class platform. Mutating in place keeps the Eightfold
        # parser as the single source of truth for field mapping.
        for job in jobs:
            object.__setattr__(job, "ats_type", ATSType.MICROSOFT)
            # ``global_id`` is computed at construction time from the
            # (then) ats_type=eightfold; recompute it now so consumers
            # see ``microsoft:<id>`` instead of ``eightfold:<id>``.
            if job.ats_id:
                object.__setattr__(
                    job, "global_id", f"{ATSType.MICROSOFT.value}:{job.ats_id}"
                )
        return jobs


__all__ = ["MicrosoftScraper"]
