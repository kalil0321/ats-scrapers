"""global_id: one id per posting, the board included where ids repeat."""

from __future__ import annotations

import uuid

import pandas as pd
import pytest
from pydantic import ValidationError

from ats_scrapers import client as client_module
from ats_scrapers.identity import TENANT_SCOPED_ATS, compose_global_id, posting_tenant
from ats_scrapers.models import ATSType, Job


def _job(ats: str, ats_id: str | None, url: str, **extra: object) -> Job:
    return Job(url=url, title="Engineer", company="Acme", ats_type=ATSType(ats), ats_id=ats_id, **extra)


# Real postings of two employers that share an id on the same provider.
SHARED_IDS = [
    ("icims", "6983", "https://careers-acme.icims.com/jobs/6983/nurse/job?in_iframe=1",
     "https://careers-globex.icims.com/jobs/6983/nurse/job?in_iframe=1"),
    ("bamboohr", "178", "https://fss.bamboohr.com/careers/178", "https://canadawestland.bamboohr.com/careers/178"),
    ("cornerstone", "3061", "https://acme.csod.com/ux/ats/careersite/4/home/requisition/3061?c=acme",
     "https://globex.csod.com/ux/ats/careersite/1/home/requisition/3061?c=globex"),
    ("phenom", "R183484", "https://careers.oreillyauto.com/us/en/job/R183484",
     "https://careers.cisco.com/global/en/job/R183484"),
    ("taleo", "7371", "https://phg.tbe.taleo.net/phg04/ats/careers/v2/viewRequisition?org=COMACOUN&cws=37&rid=7371",
     "https://phg.tbe.taleo.net/phg02/ats/careers/v2/viewRequisition?org=EMPITODA&cws=37&rid=7371"),
    ("personio", "2819185", "https://bright-works.jobs.personio.com/job/2819185",
     "https://voessing.jobs.personio.com/job/2819185"),
    ("jobvite", "oZPMAfwU", "https://jobs.jobvite.com/jbs/job/oZPMAfwU",
     "https://jobs.jobvite.com/pilgrims/job/oZPMAfwU"),
    ("beisen", "511162440", "https://imart360.zhiye.com/portal/detail?jobAdId=9e77",
     "https://cyys.zhiye.com/portal/detail?jobAdId=0b8b"),
    ("avature", "18219", "https://clinicianjobs.advocatehealth.org/careers/JobDetail/Physician/18219",
     "https://ciusss.avature.net/fr_CA/careers/JobDetail/Infirmier/18219"),
    ("eightfold", "94804", "https://morganstanley.eightfold.ai/careers/job/549795891061",
     "https://careers.newyorklife.com/careers/job/44337482"),
    ("successfactors", "1234", "https://jobs.acme.com/job/Zurich-Engineer/1234/",
     "https://careers.globex.com/job/Basel-Engineer/1234/"),
]


def test_every_tenant_scoped_provider_is_covered() -> None:
    assert {ats for ats, *_ in SHARED_IDS} == TENANT_SCOPED_ATS


@pytest.mark.parametrize(("ats", "ats_id", "first", "second"), SHARED_IDS)
def test_same_id_on_two_boards_gives_two_global_ids(ats: str, ats_id: str, first: str, second: str) -> None:
    one, two = _job(ats, ats_id, first), _job(ats, ats_id, second)
    assert one.global_id != two.global_id
    assert one.global_id.startswith(f"{ats}:") and one.global_id.endswith(f":{ats_id}")
    assert one.ats_id == two.ats_id == ats_id


@pytest.mark.parametrize(("ats", "url", "board"), [
    ("icims", "https://careers-acme.icims.com/jobs/1/x/job", "careers-acme.icims.com"),
    ("icims", "https://Careers-Acme.ICIMS.com:443/jobs/1/x/job", "careers-acme.icims.com"),
    ("phenom", "https://www.careers.acme.com/us/en/job/R1", "careers.acme.com"),
    ("taleo", "https://phg.tbe.taleo.net/phg04/ats/careers/v2/viewRequisition?org=comacoun&rid=1",
     "phg.tbe.taleo.net/COMACOUN"),
    ("taleo", "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1", "acme.taleo.net"),
    ("jobvite", "https://jobs.jobvite.com/JBS/job/oZPMAfwU", "jobs.jobvite.com/jbs"),
    ("jobvite", "https://jobs.jobvite.com/", "jobs.jobvite.com"),
    ("jobvite", "https://jobs.jobvite.com/careers/LoanDepot/job/oA1b", "jobs.jobvite.com/loandepot"),
    ("jobvite", "https://jobs.jobvite.com/loandepot/job/oA1b", "jobs.jobvite.com/loandepot"),
    ("jobvite", "https://jobs.jobvite.com/careers", "jobs.jobvite.com"),
])
def test_board_read_from_the_url(ats: str, url: str, board: str) -> None:
    assert posting_tenant(ats, url) == board


def test_the_same_posting_seen_twice_keeps_one_id() -> None:
    # A title change in the slug, or another careersite of the same employer.
    assert _job("icims", "6983", "https://careers-acme.icims.com/jobs/6983/nurse/job").global_id == _job(
        "icims", "6983", "https://careers-acme.icims.com/jobs/6983/head-nurse/job?in_iframe=1").global_id
    assert _job("cornerstone", "21352", "https://uic.csod.com/ux/ats/careersite/1/home/requisition/21352?c=uic"
                ).global_id == _job(
        "cornerstone", "21352", "https://uic.csod.com/ux/ats/careersite/2/home/requisition/21352?c=uic").global_id


@pytest.mark.parametrize(("ats", "ats_id", "url", "expected"), [
    ("greenhouse", "4012345", "https://boards.greenhouse.io/acme/jobs/4012345", "greenhouse:4012345"),
    ("workday", "acme.wd1.myworkdayjobs.com/job/USA/Engineer_R-1",
     "https://acme.wd1.myworkdayjobs.com/en-US/External/job/USA/Engineer_R-1",
     "workday:acme.wd1.myworkdayjobs.com/job/USA/Engineer_R-1"),
    ("icims", "14691", "https://careers-vhchealth.icims.com/jobs/14691/assistant/job",
     "icims:careers-vhchealth.icims.com:14691"),
    ("taleo", "7452", "https://phg.tbe.taleo.net/phg04/ats/careers/v2/viewRequisition?org=KATTMUCH2&cws=39&rid=7452",
     "taleo:phg.tbe.taleo.net/KATTMUCH2:7452"),
])
def test_global_id_format(ats: str, ats_id: str, url: str, expected: str) -> None:
    assert _job(ats, ats_id, url).global_id == expected
    assert compose_global_id(ats, ats_id, url) == expected


def test_provider_ids_that_are_global_stay_unscoped() -> None:
    first = _job("lever", "a1b2", "https://jobs.lever.co/acme/a1b2")
    second = _job("lever", "a1b2", "https://jobs.lever.co/globex/a1b2")
    assert first.global_id == second.global_id == "lever:a1b2"


def test_missing_id_still_falls_back_to_a_uuid4() -> None:
    job = _job("icims", None, "https://careers-acme.icims.com/jobs/1/x/job")
    assert uuid.UUID(job.global_id).version == 4


def test_unparsable_url_gives_no_board() -> None:
    assert posting_tenant("icims", "http://[::1") is None
    assert posting_tenant("icims", "not a url") is None
    assert compose_global_id("icims", "1", None) == "icims:1"


# --- uuid ---------------------------------------------------------------------

def test_uuid_takes_a_v7_only() -> None:
    v7 = "01936f2a-7c4e-7b3a-9f10-2c3d4e5f6a7b"
    assert _job("lever", "a", "https://jobs.lever.co/acme/a", uuid=v7).uuid == v7
    assert _job("lever", "a", "https://jobs.lever.co/acme/a").uuid is None
    for bad in (str(uuid.uuid4()), "01936F2A-7C4E-7B3A-9F10-2C3D4E5F6A7B", "nope", ""):
        with pytest.raises(ValidationError):
            _job("lever", "a", "https://jobs.lever.co/acme/a", uuid=bad)


# --- dataset files without a global_id column -----------------------------------

def test_client_rebuilds_the_model_global_id() -> None:
    rows = [(ats, ats_id, url) for ats, ats_id, url, _ in SHARED_IDS] + [
        ("greenhouse", "4012345", "https://boards.greenhouse.io/acme/jobs/4012345"),
        ("workday", "acme.wd1.myworkdayjobs.com/job/USA/E_R-1",
         "https://acme.wd1.myworkdayjobs.com/en-US/External/job/USA/E_R-1"),
    ]
    frame = pd.DataFrame(rows, columns=["ats_type", "ats_id", "url"])
    rebuilt = client_module._normalize_dataset(frame)
    assert list(rebuilt["global_id"]) == [_job(*row).global_id for row in rows]
    assert rebuilt["global_id"].is_unique


def test_client_scoped_row_without_url_keeps_the_short_id() -> None:
    frame = pd.DataFrame({"ats_type": ["icims", "icims"], "ats_id": ["1", "2"],
                          "url": ["https://careers-acme.icims.com/jobs/1/x/job", None]})
    assert list(client_module._normalize_dataset(frame)["global_id"]) == [
        "icims:careers-acme.icims.com:1", "icims:2"]


def test_client_without_url_column_keeps_the_short_id() -> None:
    frame = pd.DataFrame({"ats_type": ["icims"], "ats_id": ["1"]})
    assert list(client_module._normalize_dataset(frame)["global_id"]) == ["icims:1"]


@pytest.mark.parametrize("columns", [
    {"ats_type": ["lever"], "ats_id": ["a"], "url": ["https://jobs.lever.co/acme/a"]},
    {"ats_type": ["lever"], "ats_id": ["a"], "global_id": ["lever:a"]},
])
def test_client_adds_a_null_uuid_to_files_without_it(columns: dict) -> None:
    frame = client_module._normalize_dataset(pd.DataFrame(columns))
    assert frame["uuid"].isna().all()


def test_client_keeps_a_published_uuid() -> None:
    v7 = "01936f2a-7c4e-7b3a-9f10-2c3d4e5f6a7b"
    frame = pd.DataFrame({"ats_type": ["lever"], "ats_id": ["a"], "global_id": ["lever:a"], "uuid": [v7]})
    assert list(client_module._normalize_dataset(frame)["uuid"]) == [v7]
