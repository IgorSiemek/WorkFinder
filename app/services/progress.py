from __future__ import annotations

from datetime import date, timedelta
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Application, ApplicationActivity, ExcelSyncTask, Job, utcnow
from app.services.backups import automatic_backup
from app.services.excel_sync import mark_applied_in_excel

APPLICATION_STATUSES = (
    "Saved",
    "Preparing",
    "Applied",
    "Recruiter Screen",
    "Technical/Case Interview",
    "Hiring Manager Interview",
    "Final Interview",
    "Offer",
    "Rejected",
    "Withdrawn",
    "Expired",
)
INTERVIEW_STATUSES = {
    "Recruiter Screen",
    "Technical/Case Interview",
    "Hiring Manager Interview",
    "Final Interview",
}
CLOSED_STATUSES = {"Offer", "Rejected", "Withdrawn", "Expired"}


def record(session: Session, application: Application, kind: str, description: str) -> None:
    session.add(
        ApplicationActivity(
            application=application,
            event_type=kind,
            description=description,
        )
    )


def change_status(
    session: Session,
    job: Job,
    status: str,
    *,
    today: date | None = None,
) -> Application:
    if status not in APPLICATION_STATUSES:
        raise ValueError("Invalid application status")
    today = today or date.today()
    application = job.application
    if application is None:
        application = Application(job=job, status="Saved")
        session.add(application)
        session.flush()
        record(session, application, "status", "Application saved")
    old = application.status
    if old == status:
        return application
    application.status = status
    application.last_activity = utcnow()
    record(session, application, "status", f"Status changed from {old} to {status}")
    if status == "Preparing" and application.date_started is None:
        application.date_started = utcnow()
    if status == "Applied" and application.date_applied is None:
        application.date_applied = utcnow()
        application.application_url = job.original_url
        if application.next_action_date is None:
            application.next_action_date = today + timedelta(days=7)
            record(
                session,
                application,
                "follow_up",
                f"Follow-up set to {application.next_action_date}",
            )
        if settings.excel_jobs_path and not job.is_demo:
            task = session.scalar(
                select(ExcelSyncTask).where(
                    ExcelSyncTask.application_id == application.id,
                )
            )
            if task is None:
                session.add(
                    ExcelSyncTask(
                        application=application,
                        workbook_path=settings.excel_jobs_path,
                        applied_on=application.date_applied.date(),
                    )
                )
    if status == "Offer" and application.offer_date is None:
        application.offer_date = today
    if status == "Rejected" and application.rejection_date is None:
        application.rejection_date = today
    return application


def update_details(
    session: Session,
    application: Application,
    *,
    notes: str,
    cv_version: str,
    next_action_date: date | None,
) -> None:
    for field, new, kind, label in (
        ("notes", notes, "note", "Notes"),
        ("cv_version", cv_version or None, "cv", "CV version"),
        ("next_action_date", next_action_date, "follow_up", "Next action"),
    ):
        old = getattr(application, field)
        if old != new:
            setattr(application, field, new)
            record(session, application, kind, f"{label}: {old or '(empty)'} → {new or '(empty)'}")
            application.last_activity = utcnow()


def retry_excel_sync(session: Session, *, application_id: int | None = None) -> tuple[int, int]:
    query = select(ExcelSyncTask).where(ExcelSyncTask.status != "completed")
    if application_id is not None:
        query = query.where(ExcelSyncTask.application_id == application_id)
    tasks = session.scalars(query).all()
    completed = 0
    for task in tasks:
        task.attempts += 1
        try:
            mark_applied_in_excel(task.workbook_path, task.application.job, task.applied_on)
        except (OSError, ValueError, BadZipFile, InvalidFileException) as exc:
            task.status = "failed"
            task.error_message = str(exc)[:1000]
        else:
            task.status = "completed"
            task.error_message = None
            completed += 1
            record(session, task.application, "excel_sync", "Application synchronized to Excel")
        task.updated_at = utcnow()
    session.commit()
    return completed, len(tasks) - completed


def save_progress(session: Session, application: Application) -> str:
    # Commit status + audit + outbox together, before touching the workbook.
    session.commit()
    _, pending = retry_excel_sync(session, application_id=application.id)
    automatic_backup(session.get_bind())
    return (
        "Saved in database; Excel sync pending (retry with /excel)" if pending else "Progress saved"
    )
