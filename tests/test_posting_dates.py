from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_session
from app.main import app
from app.models import Job
from app.scrapers.base import JobPosting
from app.services.search import import_posting


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

    def override():  # noqa: ANN202
        yield session

    app.dependency_overrides[get_session] = override
    try:
        client = TestClient(app)
        found = client.get("/jobs?view=new")
        assert "Older Finance Intern" in found.text
        assert "FOUND TODAY" in found.text
        assert "NEW TODAY" not in found.text
        assert "Employer posting date unavailable" in found.text
        assert (today - timedelta(days=29)).strftime("%d %b %Y") in found.text
        recent = client.get("/jobs?posted_days=7")
        assert {job.title for job in recent.context["jobs"]} == {
            "Recent Finance Intern",
            "Today Finance Intern",
        }
        only_today = client.get("/jobs?posted_days=1")
        assert [job.title for job in only_today.context["jobs"]] == ["Today Finance Intern"]
        sorted_jobs = client.get("/jobs?sort=posted").context["jobs"]
        assert sorted_jobs[0].title == "Today Finance Intern"
        assert sorted_jobs[-1].title == "Unknown Finance Intern"
        detail = client.get(f"/jobs/{jobs[0].id}")
        assert "Employer posted" in detail.text and "Found by WorkFinder" in detail.text
        assert "Found today" in client.get("/").text
    finally:
        app.dependency_overrides.pop(get_session, None)


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
