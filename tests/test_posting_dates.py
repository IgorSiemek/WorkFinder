from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Job
from app.scrapers.base import JobPosting
from app.services.jobs import list_jobs
from app.services.search import import_posting
from app.telegram.views import job_card


def test_discovery_and_employer_posting_dates_are_separate(session: Session) -> None:
    today = date.today()
    now = datetime.now()
    jobs = [
        Job(
            source="Test",
            title=title,
            company=f"Example {i}",
            location="Kraków",
            original_url=f"https://example.com/dates/{i}",
            canonical_url=f"https://example.com/dates/{i}",
            job_hash=f"dates-{i}",
            date_discovered=now,
            date_posted=posted,
        )
        for i, (title, posted) in enumerate(
            [
                ("Older Finance Intern", today - timedelta(days=29)),
                ("Recent Finance Intern", today - timedelta(days=1)),
                ("Today Finance Intern", today),
                ("Unknown Finance Intern", None),
                ("Boundary Finance Intern", today - timedelta(days=7)),
            ]
        )
    ]
    session.add_all(jobs)
    session.commit()

    found, _ = list_jobs(session)
    assert "Older Finance Intern" in {job.title for job in found}
    card = job_card(jobs[0])["text"]
    assert f"Publikacja {today - timedelta(days=29)}" in card
    assert "Znaleziono" in card
    assert "brak daty" in job_card(jobs[3])["text"]
    recent, _ = list_jobs(session, "all", posted_days=7)
    assert {job.title for job in recent} == {"Recent Finance Intern", "Today Finance Intern"}
    only_today, _ = list_jobs(session, "all", posted_days=1)
    assert [job.title for job in only_today] == ["Today Finance Intern"]


def test_repeat_import_enriches_posting_date_without_changing_discovery(session: Session) -> None:
    posting = JobPosting(
        source="Example",
        source_job_id="R1",
        title="Finance Intern",
        company="Example",
        url="https://example.com/enriched-date",
        location="Kraków",
    )
    job, _ = import_posting(session, posting)
    session.commit()
    discovered = job.date_discovered
    posting.date_posted = date(2026, 9, 17)
    same, outcome = import_posting(session, posting)
    session.commit()
    assert outcome == "duplicate" and same.id == job.id
    assert same.date_discovered == discovered
    assert same.date_posted == date(2026, 9, 17)
