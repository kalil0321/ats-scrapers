from ats_scrapers import client, find_company, get_scraper_for_url
from ats_scrapers.scrapers import get_scraper
from ats_scrapers.models import ATSType
import argparse
import pandas as pd

def search():
    args = argparse.ArgumentParser()
    args.add_argument(
        '--query', dest='query',
        type=str, default=None, nargs='?',
    )
    args.add_argument(
        '--location', dest='location',
        type=str, default=None, nargs='?',
    )
    args.add_argument(
        '--company', dest='company',
        type=str, default=None, nargs='?',
    )
    args.add_argument(
        '--ats-type', dest='ats',
        type=str, default=None, nargs='?',
        choices=[e.name.lower() for e in ATSType],
    )
    args.add_argument(
        '--remote', dest='remote',
        action='store_true',
    )
    args.add_argument(
        '--salary-min', dest='salary_min',
        type=float, default=None, nargs='?',
    )
    args.add_argument(
        '--salary-max', dest='salary_max',
        type=float, default=None, nargs='?',
    )
    args.add_argument(
        '--experience-max', dest='experience_max',
        type=int, default=None, nargs='?',
    )
    args.add_argument(
        '--limit', dest='limit',
        type=int, default=None, nargs='?',
    )
    jobs = client.search(**args.parse_args().__dict__)
    print(jobs.to_json(orient='records', indent=4))

def find():
    args = argparse.ArgumentParser()
    args.add_argument(
        'company', type=str,
    )
    company = find_company(args.parse_args().company)
    print(company.to_json(orient='records', indent=4))

def fetch():
    args = argparse.ArgumentParser()
    args.add_argument(
        'company', type=str,
    )
    args.add_argument(
        'ats', type=str,
    )
    jobs = get_scraper(args.parse_args().ats, args.parse_args().company).fetch()
    print(jobs.to_json(orient='records', indent=4))

def fetch_for_url():
    args = argparse.ArgumentParser()
    args.add_argument(
        'url', type=str,
    )
    jobs = get_scraper_for_url(args.parse_args().url).fetch()
    print(jobs.to_json(orient='records', indent=4))
