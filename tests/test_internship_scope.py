from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Job, utcnow
from app.scrapers.base import JobPosting
from app.scrapers.common import internship_label, is_internship_role
from app.services import search
from app.services.jobs import add_manual_job, list_jobs
from app.services.matching import score_job

ROLE_CASES = [
    ("Finance Intern", "Full-time", "", True),
    ("SUMMER INTERNSHIP — Data", "", "", True),
    ("Stażysta / Stażystka w finansach", "", "", True),
    ("Stazysta ds. analiz", "", "", True),
    ("Program stażowy — Analityka", "", "", True),
    ("Płatne praktyki w bankowości", "", "", True),
    ("Praktykantka ds. controllingu", "", "", True),
    ("Graduate Finance Trainee", "", "", True),
    ("Finance Traineeship", "", "", True),
    ("Data Analyst Apprentice", "", "", True),
    ("Analytics Apprenticeship", "", "", True),
    ("Working Student — Finance", "Part-time", "", True),
    ("Student Programme — Data", "", "", True),
    ("Industrial Placement — Finance", "", "", True),
    ("Analyst", "Internship", "", True),
    ("Analityk", "", "Stażysta", True),
    ("Internal Auditor", "Full-time", "", False),
    ("International Finance Analyst", "", "", False),
    ("Junior Data Analyst", "Full-time", "Entry level", False),
    ("Graduate Analyst", "Graduate", "", False),
    ("Student Support Specialist", "", "", False),
    ("Analityk z pięcioletnim stażem", "", "", False),
    ("Internship Program Manager", "Internship", "", False),
    ("Senior Data Analyst", "Internship", "", False),
]


@pytest.mark.parametrize(("title", "employment", "seniority", "expected"), ROLE_CASES)
def test_role_scope_uses_whole_words_and_explicit_training_metadata(
    title: str, employment: str, seniority: str, expected: bool
) -> None:
    assert is_internship_role(title, employment, seniority) is expected


def make_job(title: str, index: int, **fields) -> Job:  # noqa: ANN003
    return Job(
        source="Test",
        canonical_url=f"https://example.com/{index}",
        original_url=f"https://example.com/{index}",
        title=title,
        company=f"Company {index}",
        location="Kraków",
        description="A full-time role; previous internship experience is welcome.",
        job_hash=str(index),
        **fields,
    )


def test_database_scope_matches_python_classifier(session: Session) -> None:
    for index, (title, employment, seniority, _) in enumerate(ROLE_CASES):
        session.add(make_job(title, index, employment_type=employment, seniority=seniority))
    session.commit()
    expected = {title for title, _, _, accepted in ROLE_CASES if accepted}
    actual = {job.title for job in session.scalars(select(Job).where(Job.is_internship))}
    assert actual == expected


def test_discovery_filters_existing_jobs_before_pagination_and_keeps_history(
    session: Session,
) -> None:
    ordinary = [make_job(f"Regular Analyst {i}", i, match_score=100) for i in range(15)]
    intern = make_job("English Finance Intern", 20, match_score=70)
    trainee = make_job("Polski program stażowy", 21, match_score=60)
    ignored = make_job("Archived Regular Analyst", 22, ignored=True)
    session.add_all([*ordinary, intern, trainee, ignored])
    session.flush()
    session.add(Application(job=ordinary[0], status="Saved"))
    session.commit()

    for view in ("all", "new"):
        jobs, total = list_jobs(session, view, page_size=1)
        assert total == 2
        assert len(jobs) == 1
        assert jobs[0].title == "English Finance Intern"
        assert list_jobs(session, view, page=2, page_size=1)[0][0].id == trainee.id
    assert "Regular Analyst 0" in {job.title for job in list_jobs(session, "saved")[0]}
    assert "Archived Regular Analyst" in {job.title for job in list_jobs(session, "ignored")[0]}
    assert "Regular Analyst 0" in {job.title for job in list_jobs(session, "applications")[0]}
    with pytest.raises(ValueError):
        add_manual_job(session, "Internal Auditor | Example | https://example.com/manual")
    assert session.scalar(select(Job).where(Job.title == "Internal Auditor")) is None


def test_search_rejects_regular_jobs_even_when_provider_returns_them(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    postings = [
        JobPosting(
            source="Test",
            source_job_id=str(index),
            url=f"https://provider.example/{index}",
            title=title,
            company=f"Provider Company {index}",
            location=location,
            remote_type=remote,
            description="Excel, SQL and internship experience.",
        )
        for index, (title, location, remote) in enumerate(
            [
                ("Finance Intern", "Kraków", "Hybrid"),
                ("Stażystka ds. danych", "Krakow", "On-site"),
                ("Finance Trainee", "Cracow", "On-site"),
                ("Junior Financial Analyst", "Kraków", "On-site"),
                ("Internal Auditor", "Kraków", "On-site"),
                ("Finance Intern", "Warsaw", "On-site"),
                ("Data Intern", "Kraków", "Remote"),
            ]
        )
    ]
    old_regular = make_job("Legacy Analyst", 50, last_seen=utcnow() - timedelta(days=9))
    old_intern = make_job("Legacy Intern", 51, last_seen=utcnow() - timedelta(days=9))
    session.add_all([old_regular, old_intern])
    session.commit()

    class Provider:
        name = "Test"
        can_confirm_absence = True

        def search_jobs(self, criteria):  # noqa: ANN001, ANN202
            assert "trainee" in criteria.keywords
            assert "staż" in criteria.keywords
            return postings

    monkeypatch.setattr(search, "enabled_sources", lambda: [Provider()])
    result = search.run_search(session)
    assert result.status == "completed"
    assert result.jobs_found == result.new_jobs == 3
    imported = session.scalars(select(Job).where(Job.id.not_in([old_regular.id, old_intern.id])))
    assert {job.title for job in imported} == {
        "Finance Intern",
        "Stażystka ds. danych",
        "Finance Trainee",
    }
    session.refresh(old_regular)
    session.refresh(old_intern)
    assert old_regular.is_active  # History outside the new scope is not expired by this search.
    assert not old_intern.is_active


def test_badge_distinguishes_training_opportunity_types() -> None:
    assert internship_label("Finance Intern") == "Internship"
    assert internship_label("Finance Trainee") == "Trainee"
    assert internship_label("Finance Apprentice") == "Apprenticeship"
    assert internship_label("Working Student Finance") == "Student placement"


def test_trainees_get_training_bonus_without_using_description_mentions() -> None:
    trainee = make_job("Finance Trainee", 60)
    regular = make_job("Finance Analyst", 61)
    assert {"label": "Internship or trainee level", "points": 20} in score_job(trainee).explanation
    assert "Internship or trainee level" not in {
        item["label"] for item in score_job(regular).explanation
    }
