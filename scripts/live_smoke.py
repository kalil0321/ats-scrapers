"""Live smoke test: fetch real jobs from real ATS endpoints.

Run this before every release from a machine with open outbound
network (it cannot run in sandboxed CI):

    python scripts/live_smoke.py

Exit code is non-zero when more than 3 scrapers fail, so it can gate a
release script. Pass --dataset to also exercise the hosted-dataset
client (downloads the greenhouse slice, ~tens of MB).

Hits real ATS endpoints with well-known tenants. Descriptions off where
supported to keep request counts polite. Each scraper gets a hard
timeout so one hung endpoint can't stall the run.
"""

import asyncio
import sys

from ats_scrapers.scrapers import (
    AshbyScraper,
    BambooHRScraper,
    BreezyScraper,
    GreenhouseScraper,
    LeverScraper,
    PersonioScraper,
    PinpointScraper,
    RecruiteeScraper,
    RemoteOKScraper,
    SmartRecruitersScraper,
    TeamtailorScraper,
    TheHubScraper,
    UberScraper,
    WeWorkRemotelyScraper,
    WorkableScraper,
    WorkdayScraper,
    YCombinatorScraper,
)

# (label, scraper factory) — mainstream tenants known to be live.
CASES = [
    ("greenhouse/anthropic", lambda: GreenhouseScraper("anthropic")),
    ("lever/palantir", lambda: LeverScraper("palantir")),
    ("ashby/openai", lambda: AshbyScraper("openai", include_descriptions=False)),
    ("smartrecruiters/10pearls", lambda: SmartRecruitersScraper("10pearls", include_descriptions=False)),
    ("workable/0x", lambda: WorkableScraper("0x", include_descriptions=False)),
    ("recruitee/12build", lambda: RecruiteeScraper("12build", include_descriptions=False)),
    ("teamtailor/1komma5", lambda: TeamtailorScraper("1komma5", include_descriptions=False)),
    ("breezy/10-4-truck-recruiting", lambda: BreezyScraper("10-4-truck-recruiting", include_descriptions=False)),
    ("bamboohr/10web", lambda: BambooHRScraper("10web", include_descriptions=False)),
    ("personio/10xfounders", lambda: PersonioScraper("10xfounders", include_descriptions=False)),
    ("pinpoint/aawdc", lambda: PinpointScraper("aawdc", include_descriptions=False)),
    ("workday/2020companies", lambda: WorkdayScraper.from_url(
        "https://2020companies.wd1.myworkdayjobs.com/external_careers",
        include_descriptions=False, max_fetch_seconds=90,
    )),
    ("remoteok", lambda: RemoteOKScraper("remoteok")),
    ("weworkremotely", lambda: WeWorkRemotelyScraper("weworkremotely")),
    ("thehub", lambda: TheHubScraper("thehub", include_descriptions=False)),
    ("ycombinator", lambda: YCombinatorScraper("ycombinator", include_descriptions=False)),
    ("uber", lambda: UberScraper("uber", include_descriptions=False)),
]

TIMEOUT = 150.0


async def run_case(label, factory):
    try:
        scraper = factory()
        async with asyncio.timeout(TIMEOUT):
            jobs = await scraper.afetch()
        n = len(jobs)
        if n == 0:
            return (label, "EMPTY", "0 jobs (may be a legitimately empty board)")
        j = jobs[0]
        problems = []
        if not j.title:
            problems.append("no title")
        if not j.url:
            problems.append("no url")
        if not j.global_id or (j.global_id.count("-") >= 4 and ":" not in j.global_id):
            problems.append(f"uuid-fallback global_id: {j.global_id}")
        if j.fetched_at is not None and j.fetched_at.tzinfo is None:
            problems.append("naive fetched_at")
        with_desc = sum(1 for x in jobs[:50] if x.description)
        status = "WARN" if problems else "OK"
        return (label, status,
                f"{n} jobs; sample: {j.title[:40]!r} @ {j.location!r}; "
                f"desc {with_desc}/{min(n,50)}; {'; '.join(problems) or 'fields ok'}")
    except Exception as exc:
        return (label, "FAIL", f"{type(exc).__name__}: {str(exc)[:160]}")


async def main():
    results = await asyncio.gather(*(run_case(lbl, f) for lbl, f in CASES))
    failed = 0
    for label, status, detail in results:
        print(f"{status:5s} {label:32s} {detail}")
        if status == "FAIL":
            failed += 1
    print(f"\n{len(CASES) - failed}/{len(CASES)} scrapers fetched live data")
    return failed


async def dataset_check() -> None:
    from ats_scrapers import Client, list_ats

    sources = list(list_ats())
    print(f"manifest OK: {len(sources)} sources")
    df = Client().search(query="engineer", ats="greenhouse", limit=5)
    assert not df.empty, "greenhouse slice search returned nothing"
    print(f"dataset OK: sample title {df.iloc[0]['title']!r}")


if __name__ == "__main__":
    failures = asyncio.run(main())
    if "--dataset" in sys.argv:
        asyncio.run(dataset_check())
    sys.exit(1 if failures > 3 else 0)
