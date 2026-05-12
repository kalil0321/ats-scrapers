"""Tests for the DiDi Global careers scraper.

Scope: ``_parse_job`` only — pagination + HTTP shape is covered by the
live-API verification done at scrape time, mocking ``httpx`` here would
add brittleness without protecting any contract we control.
"""

from __future__ import annotations

from datetime import datetime

from jobhive.models import ATSType
from jobhive.scrapers.didi import DidiScraper

# Two realistic fixtures captured from the live list endpoint on
# 2026-05-12. First is a Chinese-language Beijing posting (the bulk of
# the catalogue), second is an English-language posting that exercises
# the CJK→language fall-through to ``en`` and demonstrates that
# ``country_iso`` is left None when no CJK is present in workArea.
_FIXTURE_ZH = {
    "id": None,
    "jdId": 63733,
    "jdNo": "JR2026051100R",
    "recruitType": None,
    "workArea": "北京市",
    "deptName": "Fintech Technology",
    "deptCode": None,
    "jobType": 3,
    "jobName": "AI 客服产品经理 (JR2026051100R)",
    "createTime": 1747000000000,
    "labelCode": "AI",
    "labelName": "",
    "labels": ["ai", "fintech"],
    "refreshTime": "2026-05-11 22:39:45",
    "jobTypeName": "产品",
    "jobDuty": "负责 AI 客服核心产品层的设计与推进。",
    "jobQualification": "有智能客服、对话机器人产品经验。",
    "isUrgent": 1,
    "channelId": 7,
    "jobLevel": "P6",
    "recruiterLdap": None,
    "recruiterName": None,
    "new": False,
}

_FIXTURE_EN = {
    "id": None,
    "jdId": 58739,
    "jdNo": "J250827001",
    "recruitType": None,
    "workArea": "Mexico City",
    "deptName": "Project X",
    "deptCode": None,
    "jobType": 5,
    "jobName": "Growth Operation Manager (J250827001)",
    "createTime": None,
    "labelCode": None,
    "labelName": "",
    "labels": None,
    "refreshTime": "2026-05-11 22:19:23",
    "jobTypeName": None,
    "jobDuty": None,
    "jobQualification": None,
    "isUrgent": None,
    "channelId": None,
    "jobLevel": None,
    "new": False,
}


# --- _parse_job: Chinese fixture --------------------------------------------


def test_parse_job_chinese_uses_jdid_as_ats_id() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH, recruit_type=1)
    assert job is not None
    assert job.ats_type is ATSType.DIDI
    assert job.ats_id == "63733"
    assert job.global_id == "didi:63733"


def test_parse_job_chinese_url_uses_jdid() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH)
    assert job is not None
    assert str(job.url) == "https://talent.didiglobal.com/social/p/63733"


def test_parse_job_chinese_language_is_zh() -> None:
    """CJK characters in the title → language='zh'."""
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH)
    assert job is not None
    assert job.language == "zh"


def test_parse_job_chinese_workarea_sets_country_iso_cn() -> None:
    """Chinese city name in workArea triggers the CN heuristic."""
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH)
    assert job is not None
    assert job.country_iso == "CN"
    assert job.location == "北京市"


def test_parse_job_requisition_id_from_jdno() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH)
    assert job is not None
    assert job.requisition_id == "JR2026051100R"


def test_parse_job_posted_at_from_create_time_epoch_ms() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH)
    assert job is not None
    assert job.posted_at == datetime.fromtimestamp(1747000000000 / 1000)


def test_parse_job_description_concatenates_duty_and_qualification() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH)
    assert job is not None
    assert job.description is not None
    assert "负责 AI 客服核心产品层的设计与推进。" in job.description
    assert "有智能客服、对话机器人产品经验。" in job.description


def test_parse_job_department_from_job_type_name() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH)
    assert job is not None
    assert job.department == "产品"
    assert job.team == "Fintech Technology"


def test_parse_job_raw_carries_provider_specific_fields() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_ZH, recruit_type=1)
    assert job is not None
    assert job.raw is not None
    assert job.raw["label_codes"] == "AI"
    assert job.raw["labels"] == ["ai", "fintech"]
    assert job.raw["is_urgent"] == 1
    assert job.raw["channel_id"] == 7
    assert job.raw["job_level"] == "P6"
    # recruit_type falls back to the surface we were iterating when
    # the per-item field is null.
    assert job.raw["recruit_type"] == 1


# --- _parse_job: English fixture --------------------------------------------


def test_parse_job_english_language_is_en() -> None:
    """ASCII-only title → language='en'."""
    job = DidiScraper("didi")._parse_job(_FIXTURE_EN, recruit_type=1)
    assert job is not None
    assert job.language == "en"


def test_parse_job_english_workarea_leaves_country_iso_none() -> None:
    """No CJK in workArea — leave country_iso None for downstream
    LLM enrichment to fill from the free-text city name."""
    job = DidiScraper("didi")._parse_job(_FIXTURE_EN)
    assert job is not None
    assert job.country_iso is None
    assert job.location == "Mexico City"


def test_parse_job_english_null_create_time_falls_back_to_refresh_time() -> None:
    """createTime is null in the list payload for most rows; we fall
    back to the human-readable refreshTime string."""
    job = DidiScraper("didi")._parse_job(_FIXTURE_EN)
    assert job is not None
    assert job.posted_at == datetime(2026, 5, 11, 22, 19, 23)


def test_parse_job_english_description_is_none_when_both_fields_null() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_EN)
    assert job is not None
    assert job.description is None


def test_parse_job_english_requisition_id_from_jdno() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_EN)
    assert job is not None
    assert job.requisition_id == "J250827001"


def test_parse_job_company_is_didi() -> None:
    job = DidiScraper("didi")._parse_job(_FIXTURE_EN)
    assert job is not None
    assert job.company == "DiDi"


# --- _parse_job: edge cases -------------------------------------------------


def test_parse_job_returns_none_when_jdid_and_id_missing() -> None:
    assert DidiScraper("didi")._parse_job({"jobName": "Engineer"}) is None


def test_parse_job_returns_none_when_title_missing() -> None:
    assert DidiScraper("didi")._parse_job({"jdId": 1, "jobName": ""}) is None


def test_parse_job_falls_back_to_id_when_jdid_missing() -> None:
    """``jdId`` is preferred, but if a future API version drops it and
    only exposes ``id``, the scraper should still produce a row."""
    job = DidiScraper("didi")._parse_job(
        {"id": 99, "jobName": "Backend Engineer", "workArea": "Tokyo"}
    )
    assert job is not None
    assert job.ats_id == "99"
