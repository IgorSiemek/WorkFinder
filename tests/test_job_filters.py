from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.models import Application, Job
from app.services import progress
from app.services.jobs import job_action, list_jobs


def _job(title: str, age_days: int) -> Job:
    slug = title.casefold().replace(" ", "-")
    return Job(
        source="Test",
        source_job_id=slug,
        canonical_url=f"https://example.com/{slug}",
        original_url=f"https://example.com/{slug}",
        title=title,
        company="Example",
        location="Kraków",
        description="Finance internship",
        employment_type="Internship",
        job_hash=slug,
        date_discovered=datetime.now() - timedelta(days=age_days),
    )


def test_new_jobs_hide_applied_and_support_day_window(session: Session, monkeypatch) -> None:  # noqa: ANN001
    recent = _job("Recent Open Role", 1)
    applied = _job("Recent Applied Role", 1)
    ignore_from_new = _job("Ignore From New", 1)
    old = _job("Older Open Role", 20)
    session.add_all([recent, applied, ignore_from_new, old])
    session.flush()
    session.add(Application(job=applied, status="Applied"))
    session.commit()

    jobs, total = list_jobs(session)
    assert {job.title for job in jobs} == {"Recent Open Role", "Ignore From New"}
    assert total == 2
    jobs, _ = list_jobs(session, days=0, hide_applied=False)
    assert {job.title for job in jobs} == {
        "Recent Open Role",
        "Ignore From New",
        "Recent Applied Role",
        "Older Open Role",
    }
    jobs, _ = list_jobs(session, "all", days=3, hide_applied=False)
    assert len(jobs) == 3
    job_action(session, ignore_from_new.id, "ignore")
    session.commit()
    assert "Ignore From New" not in {job.title for job in list_jobs(session)[0]}
    calls = []

    def fake_excel_sync(path, job, applied_on):
        calls.append((path, job.id, applied_on))
        return SimpleNamespace(created=False, row_number=4)

    monkeypatch.setattr(progress, "mark_applied_in_excel", fake_excel_sync)
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path="test.xlsx"))
    job_action(session, recent.id, "apply")
    progress.save_progress(session, recent.application)
    assert recent.application.status == "Applied"
    assert recent.application.date_applied is not None
    assert len(calls) == 1
