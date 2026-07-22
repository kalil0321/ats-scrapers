"""Search the public dataset for ML engineering jobs in Paris."""

from ats_scrapers import search

df = search(query="machine learning", location="Paris", ats="greenhouse", limit=10)
print(df[["title", "company", "location", "salary_summary"]])
