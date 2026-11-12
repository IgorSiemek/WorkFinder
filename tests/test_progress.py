from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ApplicationActivity, ExcelSyncTask, Job
from app.services import progress
from app.services.jobs import edit_application, follow_ups, job_action


@pytest.fixture
def job(session: Session) -> Job:
    item = Job(
        source="Test",
        title="Finance Intern",
        company="Example",
        location="Kraków",
        original_url="https://example.com/progress",
        canonical_url="https://example.com/progress",
        job_hash="progress",
        description="Finance internship",
        match_score=80,
    )
    session.add(item)
    session.commit()
    return item


def test_applied_works_without_excel_and_suggests_follow_up(session, job, monkeypatch):
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path=""))
    job_action(session, job.id, "apply")
    progress.save_progress(session, job.application)
    application = job.application
    assert application.status == "Applied"
    assert application.date_applied is not None
    assert application.next_action_date == date.today() + timedelta(days=7)
    assert session.scalar(select(func.count(ExcelSyncTask.id))) == 0
    assert follow_ups(session, application.next_action_date)[0].id == application.id


def test_excel_failure_preserves_progress_and_can_retry_after_reload(session, job, monkeypatch):
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path="locked.xlsx"))

    def locked(*args):
        raise PermissionError("Workbook is open")

    monkeypatch.setattr(progress, "mark_applied_in_excel", locked)
    job_action(session, job.id, "apply")
    assert "pending" in progress.save_progress(session, job.application)
    session.expire_all()
    assert job.application.status == "Applied"
    applied_at = job.application.date_applied
    task = session.scalar(select(ExcelSyncTask))
    assert (task.status, task.attempts) == ("failed", 1)
    assert "Workbook is open" in task.error_message
    calls = []
    monkeypatch.setattr(progress, "mark_applied_in_excel", lambda *args: calls.append(args))
    assert progress.retry_excel_sync(session) == (1, 0)
    session.refresh(task)
    assert (task.status, task.attempts, task.error_message) == ("completed", 2, None)
    assert job.application.date_applied == applied_at
    assert len(calls) == 1
    progress.retry_excel_sync(session)
    assert len(calls) == 1


def test_notes_cv_and_date_changes_are_audited_without_duplicate_events(session, job):
    fields = dict(
        status="Preparing",
        notes="Contact recruiter",
        cv_version="Finance v2",
        next_action=date.today().isoformat(),
    )
    edit_application(session, job.id, **fields)
    session.commit()
    assert job.application.date_started is not None
    events = session.scalars(select(ApplicationActivity)).all()
    assert {item.event_type for item in events} >= {"status", "note", "cv", "follow_up"}
    count = len(events)
    last_activity = job.application.last_activity
    edit_application(session, job.id, **fields)
    session.commit()
    assert session.scalar(select(func.count(ApplicationActivity.id))) == count
    assert job.application.last_activity == last_activity
    assert len(follow_ups(session, date.today())) == 1
    fields["notes"] = ""
    edit_application(session, job.id, **fields)
    session.commit()
    notes = session.scalars(
        select(ApplicationActivity).where(ApplicationActivity.event_type == "note")
    ).all()
    assert len(notes) == 2
    assert "Contact recruiter" in notes[-1].description


def test_status_validation_and_outcome_dates(session, job, monkeypatch):
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path=""))
    job_action(session, job.id, "save")
    with pytest.raises(ValueError):
        edit_application(session, job.id, status="nonsense")
    assert job.application.status == "Saved"
    edit_application(session, job.id, status="Applied")
    assert job.application.date_applied is not None
    edit_application(session, job.id, status="Offer")
    session.commit()
    assert job.application.offer_date == date.today()
    assert follow_ups(session, date.today() + timedelta(days=30)) == []
    edit_application(session, job.id, status="Rejected")
    assert job.application.rejection_date == date.today()


def test_invalid_date_does_not_partially_create_application(session, job):
    with pytest.raises(ValueError):
        edit_application(session, job.id, status="Applied", next_action="2026-99-99")
    assert job.application is None
