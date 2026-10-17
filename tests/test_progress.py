from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_session
from app.main import app
from app.models import ApplicationActivity, ExcelSyncTask, Job
from app.services import progress


@pytest.fixture
def client(session: Session):  # noqa: ANN201
    def override():  # noqa: ANN202
        yield session

    app.dependency_overrides[get_session] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


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


def test_applied_works_without_excel_and_suggests_follow_up(
    session: Session, client: TestClient, job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path=""))
    response = client.post(f"/actions/jobs/{job.id}/mark-applied", follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    application = job.application
    assert application.status == "Applied"
    assert application.date_applied is not None
    assert application.next_action_date == date.today() + timedelta(days=7)
    assert session.scalar(select(func.count(ExcelSyncTask.id))) == 0
    assert "Finance Intern" in client.get("/activity").text
    page = client.get("/applications")
    assert page.context["progress"]["applied"] == 1
    assert page.context["follow_ups"][0].id == application.id


def test_excel_failure_preserves_progress_and_can_retry_after_reload(
    session: Session, client: TestClient, job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path="locked.xlsx"))

    def locked(*args):  # noqa: ANN002, ANN202
        raise PermissionError("Workbook is open")

    monkeypatch.setattr(progress, "mark_applied_in_excel", locked)
    response = client.post(f"/actions/jobs/{job.id}/mark-applied", follow_redirects=False)
    assert "pending" in response.headers["location"]
    session.expire_all()
    assert job.application.status == "Applied"
    applied_at = job.application.date_applied
    task = session.scalar(select(ExcelSyncTask))
    assert (task.status, task.attempts) == ("failed", 1)
    assert "Workbook is open" in client.get("/settings").text

    calls = []
    monkeypatch.setattr(progress, "mark_applied_in_excel", lambda *args: calls.append(args))
    retry = client.post("/actions/excel/retry", follow_redirects=False)
    assert retry.status_code == 303
    session.refresh(task)
    assert (task.status, task.attempts, task.error_message) == ("completed", 2, None)
    assert job.application.date_applied == applied_at
    assert len(calls) == 1
    client.post("/actions/excel/retry", follow_redirects=False)
    assert len(calls) == 1  # Completed tasks are not replayed.


def test_notes_cv_and_date_changes_are_audited_without_duplicate_events(
    session: Session, client: TestClient, job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path=""))
    fields = {
        "status": "Preparing",
        "notes": "Contact recruiter",
        "cv_version": "Finance v2",
        "next_action_date": date.today().isoformat(),
    }
    client.post(f"/actions/jobs/{job.id}/application", data=fields, follow_redirects=False)
    session.expire_all()
    assert job.application.date_started is not None
    events = session.scalars(select(ApplicationActivity).order_by(ApplicationActivity.id)).all()
    assert {item.event_type for item in events} >= {"status", "note", "cv", "follow_up"}
    count = len(events)
    last_activity = job.application.last_activity
    client.post(f"/actions/jobs/{job.id}/application", data=fields, follow_redirects=False)
    session.expire_all()
    assert session.scalar(select(func.count(ApplicationActivity.id))) == count
    assert job.application.last_activity == last_activity
    assert client.get("/applications").context["progress"]["due"] == 1

    fields["notes"] = ""
    client.post(f"/actions/jobs/{job.id}/application", data=fields, follow_redirects=False)
    notes = session.scalars(
        select(ApplicationActivity).where(ApplicationActivity.event_type == "note")
    ).all()
    assert len(notes) == 2
    assert "Contact recruiter" in notes[-1].description


def test_kanban_validates_status_and_records_outcome_dates(
    session: Session, client: TestClient, job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(progress, "settings", SimpleNamespace(excel_jobs_path=""))
    client.post(f"/actions/jobs/{job.id}/save", follow_redirects=False)
    application = job.application
    url = f"/actions/applications/{application.id}/status"
    client.post(url, data={"status": "nonsense"}, follow_redirects=False)
    assert application.status == "Saved"
    client.post(url, data={"status": "Applied"}, follow_redirects=False)
    assert application.date_applied is not None
    client.post(url, data={"status": "Offer"}, follow_redirects=False)
    assert application.offer_date == date.today()
    assert client.get("/applications").context["follow_ups"] == []
    client.post(url, data={"status": "Rejected"}, follow_redirects=False)
    assert application.rejection_date == date.today()


def test_invalid_date_does_not_partially_create_application(
    client: TestClient,
    job: Job,
) -> None:
    client.post(
        f"/actions/jobs/{job.id}/application",
        data={
            "status": "Applied",
            "next_action_date": "2026-99-99",
        },
        follow_redirects=False,
    )
    assert job.application is None
