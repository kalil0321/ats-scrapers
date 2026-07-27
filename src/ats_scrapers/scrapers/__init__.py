"""ATS scrapers — one class per platform.

Each scraper is a thin, dependency-light fetch+parse layer that returns
`Job` instances. Discovery, enrichment, deduplication, and publishing are
kept outside the public scraper API so each scraper stays usable on its own.

>>> from ats_scrapers.scrapers import GreenhouseScraper
>>> jobs = GreenhouseScraper("anthropic").fetch()
"""

from ats_scrapers.scrapers.adp import ADPWorkforceNowScraper
from ats_scrapers.scrapers.amazon import AmazonScraper
from ats_scrapers.scrapers.apple import AppleScraper
from ats_scrapers.scrapers.arbetsformedlingen import ArbetsformedlingenScraper
from ats_scrapers.scrapers.ashby import AshbyScraper
from ats_scrapers.scrapers.avature import AvatureScraper
from ats_scrapers.scrapers.bamboohr import BambooHRScraper
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry, get_scraper
from ats_scrapers.scrapers.beisen import BeisenScraper
from ats_scrapers.scrapers.beisen_legacy import BeisenLegacyScraper
from ats_scrapers.scrapers.breezy import BreezyScraper
from ats_scrapers.scrapers.builtin import BuiltInScraper
from ats_scrapers.scrapers.bundesagentur import BundesagenturScraper
from ats_scrapers.scrapers.bytedance import BytedanceScraper
from ats_scrapers.scrapers.cornerstone import CornerstoneScraper
from ats_scrapers.scrapers.darwinbox import DarwinboxScraper
from ats_scrapers.scrapers.dayforce import DayforceScraper
from ats_scrapers.scrapers.eightfold import EightfoldScraper
from ats_scrapers.scrapers.eures import EuresScraper
from ats_scrapers.scrapers.gem import GemScraper
from ats_scrapers.scrapers.getonbrd import GetOnBrdScraper
from ats_scrapers.scrapers.google import GoogleScraper
from ats_scrapers.scrapers.greenhouse import GreenhouseScraper
from ats_scrapers.scrapers.gupy import GupyScraper
from ats_scrapers.scrapers.icims import iCIMSScraper
from ats_scrapers.scrapers.infojobs_es import InfoJobsSpainScraper
from ats_scrapers.scrapers.jazzhr import JazzHRScraper
from ats_scrapers.scrapers.jobbankca import JobBankCAScraper
from ats_scrapers.scrapers.jobs_cz import JobsCzScraper
from ats_scrapers.scrapers.jobsch import JobsChScraper
from ats_scrapers.scrapers.jobvite import JobviteScraper
from ats_scrapers.scrapers.join_com import JoinComScraper
from ats_scrapers.scrapers.lever import LeverScraper
from ats_scrapers.scrapers.manfred import ManfredScraper
from ats_scrapers.scrapers.mercor import MercorScraper
from ats_scrapers.scrapers.meta import MetaScraper
from ats_scrapers.scrapers.moka import MokaScraper
from ats_scrapers.scrapers.oracle import OracleScraper
from ats_scrapers.scrapers.pageup import PageUpScraper
from ats_scrapers.scrapers.personio import PersonioScraper
from ats_scrapers.scrapers.phenom import PhenomScraper
from ats_scrapers.scrapers.pinpoint import PinpointScraper
from ats_scrapers.scrapers.programathor import ProgramathorScraper
from ats_scrapers.scrapers.recruitee import RecruiteeScraper
from ats_scrapers.scrapers.recruiterbox import RecruiterboxScraper
from ats_scrapers.scrapers.remoteok import RemoteOKScraper
from ats_scrapers.scrapers.rippling import RipplingScraper
from ats_scrapers.scrapers.seek import SeekScraper
from ats_scrapers.scrapers.smartrecruiters import SmartRecruitersScraper
from ats_scrapers.scrapers.successfactors import SuccessFactorsScraper
from ats_scrapers.scrapers.taleo import TaleoScraper
from ats_scrapers.scrapers.teamtailor import TeamtailorScraper
from ats_scrapers.scrapers.tesla import TeslaScraper
from ats_scrapers.scrapers.thehub import TheHubScraper
from ats_scrapers.scrapers.tiktok import TikTokScraper
from ats_scrapers.scrapers.uber import UberScraper
from ats_scrapers.scrapers.ukg import UKGProScraper
from ats_scrapers.scrapers.usajobs import USAJobsScraper
from ats_scrapers.scrapers.wanted import WantedScraper
from ats_scrapers.scrapers.welcometothejungle import WTTJScraper
from ats_scrapers.scrapers.wellfound import WellfoundScraper
from ats_scrapers.scrapers.weworkremotely import WeWorkRemotelyScraper
from ats_scrapers.scrapers.workable import WorkableScraper
from ats_scrapers.scrapers.workday import WorkdayScraper
from ats_scrapers.scrapers.ycombinator import YCombinatorScraper

__all__ = [
    "ADPWorkforceNowScraper",
    "AmazonScraper",
    "AppleScraper",
    "ArbetsformedlingenScraper",
    "AshbyScraper",
    "AvatureScraper",
    "BambooHRScraper",
    "BaseScraper",
    "BeisenLegacyScraper",
    "BeisenScraper",
    "BreezyScraper",
    "BuiltInScraper",
    "BundesagenturScraper",
    "BytedanceScraper",
    "CornerstoneScraper",
    "DarwinboxScraper",
    "DayforceScraper",
    "EightfoldScraper",
    "EuresScraper",
    "GemScraper",
    "GetOnBrdScraper",
    "GoogleScraper",
    "GreenhouseScraper",
    "GupyScraper",
    "InfoJobsSpainScraper",
    "JazzHRScraper",
    "JobBankCAScraper",
    "JobsChScraper",
    "JobsCzScraper",
    "JobviteScraper",
    "JoinComScraper",
    "LeverScraper",
    "ManfredScraper",
    "MercorScraper",
    "MetaScraper",
    "MokaScraper",
    "OracleScraper",
    "PageUpScraper",
    "PersonioScraper",
    "PhenomScraper",
    "PinpointScraper",
    "ProgramathorScraper",
    "RecruiteeScraper",
    "RecruiterboxScraper",
    "RemoteOKScraper",
    "RipplingScraper",
    "ScraperRegistry",
    "SeekScraper",
    "SmartRecruitersScraper",
    "SuccessFactorsScraper",
    "TaleoScraper",
    "TeamtailorScraper",
    "TeslaScraper",
    "TheHubScraper",
    "TikTokScraper",
    "UKGProScraper",
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
