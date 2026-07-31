from ats_scrapers import client
from ats_scrapers.models import ATSType
import argparse
import pandas as pd

def search():
    args = argparse.ArgumentParser()
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
        choices=[e.name for e in ATSType],
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
    with pd.option_context(
        'display.max_rows', None,
        'display.max_columns', None,
        'display.precision', 3,
    ):
        print(client.search(**args.parse_args().__dict__))
