from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import CandidateProfile, CompanyPreference, IgnoreRule, SavedSearch
from app.scrapers.base import JobPosting
from app.services.bot_config import (
    apply_config,
    export_config,
    load_config,
    next_search,
    validate_config,
)
from app.services.jobs import job_action, list_jobs
from app.services.matching import MANUAL_SCORE_FLOOR_KEY
from app.services.search import import_posting
from app.services.seed import seed_defaults
from app.telegram.runtime import process_lock
from app.telegram.views import job_card


@pytest.mark.parametrize(
    "raw",
    [
        {"minimum_score": 101},
        {"minimum_score": True},
        {"page_size": 0},
        {"search_times": ["25:00"]},
        {"search_times": "09:00"},
        {"timezone": "missing-zone"},
        {"unexpected": "field"},
        {"profile": {"skills": "Excel"}},
        {"sources": ["missing-provider"]},
        {"profile": {"preferred_locations": ["Warszawa"]}},
        {"ignore_rules": [{"rule_type": "invalid", "value": "term"}]},
        {"searches": [{"name": "one", "minimum_score": -1}]},
        {"companies": [{"company": "a"}, {"company": "a"}]},
    ],
)
def test_invalid_config_is_rejected(raw):
    with pytest.raises(ValueError):
        validate_config(raw)


def test_export_roundtrip_preserves_existing_settings_and_wont_overwrite(session, tmp_path):
    seed_defaults(session)
    profile = session.scalar(select(CandidateProfile))
    profile.name = "Existing profile"
    profile.skills = ["Excel", "SQL"]
    session.add(IgnoreRule(rule_type="keyword", value="sales"))
    session.commit()
    path = tmp_path / "config.json"
    export_config(session, path)
    config = load_config(path)
    assert config["profile"]["name"] == "Existing profile"
    assert config["profile"]["skills"] == ["Excel", "SQL"]
    assert config["ignore_rules"][0]["value"] == "sales"
    assert "TELEGRAM_BOT_TOKEN" not in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_config(session, path)


def test_apply_is_repeatable_and_empty_sections_deactivate_without_deleting_history(session):
    seed_defaults(session)
    old_searches = session.scalars(select(SavedSearch)).all()
    config = validate_config({"profile": {"skills": ["Excel"]}, "searches": [], "companies": []})
    apply_config(session, config)
    session.commit()
    apply_config(session, config)
    session.commit()
    assert session.scalar(select(CandidateProfile)).skills == ["Excel"]
    assert all(not search.enabled for search in old_searches)
    assert not session.scalar(select(CompanyPreference).where(CompanyPreference.watched))
    assert len(session.scalars(select(SavedSearch)).all()) == len(old_searches)


def test_validation_precedes_any_database_changes(session):
    seed_defaults(session)
    profile = session.scalar(select(CandidateProfile))
    previous = profile.name
    with pytest.raises(ValueError):
        apply_config(session, {"profile": {"name": "changed"}, "minimum_score": -1})
    assert profile.name == previous


def test_saved_future_opportunity_keeps_manual_rating_after_config_reload(session):
    posting = JobPosting(
        source="ABB Careers",
        source_job_id="R1",
        url="https://example.com/jobs/R1",
        title="Master Data Internship",
        company="ABB",
        location="Krakow",
        description="This role is part of our talent pipeline.",
        raw_payload={MANUAL_SCORE_FLOOR_KEY: 100},
    )
    job, _ = import_posting(session, posting)
    assert job.match_score == 48
    job.raw_payload = {**job.raw_payload, MANUAL_SCORE_FLOOR_KEY: 85}
    job.is_active = False
    job_action(session, job.id, "save")
    session.commit()

    apply_config(session, {})
    session.commit()

    assert job.match_score == 85
    assert list_jobs(session, "saved")[0] == [job]
    assert list_jobs(session, "new", minimum_score=60)[0] == []
    assert "Nabór przyszły" in job_card(job)["text"]


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 1, 10, 7), datetime(2026, 1, 10, 8)),
        (datetime(2026, 7, 10, 6), datetime(2026, 7, 10, 7)),
        (datetime(2026, 3, 28, 20), datetime(2026, 3, 29, 7)),
        (datetime(2026, 10, 24, 20), datetime(2026, 10, 25, 8)),
    ],
)
def test_schedule_uses_local_time_across_dst(now, expected):
    assert next_search(validate_config({"search_times": ["09:00"]}), now) == expected
    assert next_search(validate_config({"search_times": []}), now) is None


def test_invalid_json_has_useful_location(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"minimum_score": }', encoding="utf-8")
    with pytest.raises(ValueError, match="wierszu 1"):
        load_config(path)


def test_lock_is_exclusive_and_released_after_exit(tmp_path):
    path = tmp_path / "bot.lock"
    with process_lock(path), pytest.raises(ValueError), process_lock(path):
        pass
    with process_lock(path):
        pass
