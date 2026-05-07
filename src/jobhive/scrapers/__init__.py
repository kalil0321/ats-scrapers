"""ATS scrapers — one class per platform.

Each scraper is a thin, dependency-light fetch+parse layer that returns
`Job` instances. Heavy lifting (discovery, enrichment, deduplication) lives
in `jobhive.pipeline` so a scraper stays usable on its own.

>>> from jobhive.scrapers import GreenhouseScraper
>>> jobs = GreenhouseScraper("openai").fetch()
"""

from jobhive.scrapers.amazon import AmazonScraper
from jobhive.scrapers.apple import AppleScraper
from jobhive.scrapers.arbetsformedlingen import ArbetsformedlingenScraper
from jobhive.scrapers.ashby import AshbyScraper
from jobhive.scrapers.avature import AvatureScraper
from jobhive.scrapers.bamboohr import BambooHRScraper
from jobhive.scrapers.base import BaseScraper, ScraperRegistry, get_scraper
from jobhive.scrapers.breezy import BreezyScraper
from jobhive.scrapers.builtin import BuiltInScraper
from jobhive.scrapers.bundesagentur import BundesagenturScraper
from jobhive.scrapers.cornerstone import CornerstoneScraper
from jobhive.scrapers.eightfold import EightfoldScraper
from jobhive.scrapers.eures import EuresScraper
from jobhive.scrapers.gem import GemScraper
from jobhive.scrapers.getonbrd import GetOnBrdScraper
from jobhive.scrapers.google import GoogleScraper
from jobhive.scrapers.greenhouse import GreenhouseScraper
from jobhive.scrapers.icims import iCIMSScraper
from jobhive.scrapers.jazzhr import JazzHRScraper
from jobhive.scrapers.jobsch import JobsChScraper
from jobhive.scrapers.join_com import JoinComScraper
from jobhive.scrapers.lever import LeverScraper
from jobhive.scrapers.manfred import ManfredScraper
from jobhive.scrapers.mercor import MercorScraper
from jobhive.scrapers.meta import MetaScraper
from jobhive.scrapers.oracle import OracleScraper
from jobhive.scrapers.personio import PersonioScraper
from jobhive.scrapers.phenom import PhenomScraper
from jobhive.scrapers.pinpoint import PinpointScraper
from jobhive.scrapers.programathor import ProgramathorScraper
from jobhive.scrapers.recruitee import RecruiteeScraper
from jobhive.scrapers.recruiterbox import RecruiterboxScraper
from jobhive.scrapers.remoteok import RemoteOKScraper
from jobhive.scrapers.rippling import RipplingScraper
from jobhive.scrapers.smartrecruiters import SmartRecruitersScraper
from jobhive.scrapers.successfactors import SuccessFactorsScraper
from jobhive.scrapers.taleo import TaleoScraper
from jobhive.scrapers.teamtailor import TeamtailorScraper
from jobhive.scrapers.tesla import TeslaScraper
from jobhive.scrapers.thehub import TheHubScraper
from jobhive.scrapers.themuse import TheMuseScraper
from jobhive.scrapers.tiktok import TikTokScraper
from jobhive.scrapers.uber import UberScraper
from jobhive.scrapers.usajobs import USAJobsScraper
from jobhive.scrapers.wanted import WantedScraper
from jobhive.scrapers.welcometothejungle import WTTJScraper
from jobhive.scrapers.wellfound import WellfoundScraper
from jobhive.scrapers.weworkremotely import WeWorkRemotelyScraper
from jobhive.scrapers.workable import WorkableScraper
from jobhive.scrapers.workday import WorkdayScraper
from jobhive.scrapers.ycombinator import YCombinatorScraper

__all__ = [
    "AmazonScraper",
    "AppleScraper",
    "ArbetsformedlingenScraper",
    "AshbyScraper",
    "AvatureScraper",
    "BambooHRScraper",
    "BaseScraper",
    "BreezyScraper",
    "BuiltInScraper",
    "BundesagenturScraper",
    "CornerstoneScraper",
    "EightfoldScraper",
    "EuresScraper",
    "GemScraper",
    "GetOnBrdScraper",
    "GoogleScraper",
    "GreenhouseScraper",
    "JazzHRScraper",
    "JobsChScraper",
    "JoinComScraper",
    "LeverScraper",
    "ManfredScraper",
    "MercorScraper",
    "MetaScraper",
    "OracleScraper",
    "PersonioScraper",
    "PhenomScraper",
    "PinpointScraper",
    "ProgramathorScraper",
    "RecruiteeScraper",
    "RecruiterboxScraper",
    "RemoteOKScraper",
    "RipplingScraper",
    "ScraperRegistry",
    "SmartRecruitersScraper",
    "SuccessFactorsScraper",
    "TaleoScraper",
    "TeamtailorScraper",
    "TeslaScraper",
    "TheHubScraper",
    "TheMuseScraper",
    "TikTokScraper",
    "USAJobsScraper",
    "UberScraper",
    "WTTJScraper",
    "WantedScraper",
    "WeWorkRemotelyScraper",
    "WellfoundScraper",
    "WorkableScraper",
    "WorkdayScraper",
    "YCombinatorScraper",
    "get_scraper",
    "iCIMSScraper",
]
