"""Scrape OpenAI's open positions from Ashby (no auth required)."""

from ats_scrapers.scrapers import AshbyScraper

scraper = AshbyScraper("openai")
jobs = scraper.fetch()

print(f"Found {len(jobs)} open positions at OpenAI")
for job in jobs[:5]:
    print(f"  · {job.title:60s}  {job.location or '-'}")
