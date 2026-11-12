from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Application, CandidateProfile, Job, utcnow
from app.scrapers.base import JobPosting
from app.scrapers.common import is_internship_role
from app.services.normalization import canonicalize_url
from app.services.progress import (
    APPLICATION_STATUSES,
    CLOSED_STATUSES,
    change_status,
    update_details,
)
from app.services.search import import_posting

APPLIED_STATUSES = set(APPLICATION_STATUSES) - {"Saved", "Preparing"}


def list_jobs(
    session: Session,
    view: str = "new",
    *,
    page: int = 1,
    page_size: int = 5,
    minimum_score: int = 0,
    days: int | None = None,
    posted_days: int = 0,
    hide_applied: bool = True,
    query: str = "",
    today: date | None = None,
) -> tuple[list[Job], int]:
    filters = [Job.is_demo.is_(False)]
    if view == "ignored":
        filters.append(Job.ignored.is_(True))
    elif view == "applications":
        filters.append(Job.application.has())
    else:
        filters.append(Job.ignored.is_(False))
        if view == "saved":
            filters.append(Job.application.has(Application.status.in_(["Saved", "Preparing"])))
        else:
            filters.extend([Job.is_internship, Job.is_active.is_(True)])
            if hide_applied:
                filters.append(~Job.application.has(Application.status.in_(APPLIED_STATUSES)))
            filters.append(Job.match_score >= minimum_score)
    selected_days = 7 if days is None and view == "new" else days
    if selected_days:
        filters.append(Job.date_discovered >= utcnow() - timedelta(days=selected_days))
    if posted_days:
        today = today or date.today()
        filters.extend(
            [
                Job.date_posted >= today - timedelta(days=posted_days - 1),
                Job.date_posted <= today,
            ]
        )
    if query:
        pattern = f"%{query}%"
        filters.append(or_(Job.title.ilike(pattern), Job.company.ilike(pattern)))
    total = session.scalar(select(func.count(Job.id)).where(*filters)) or 0
    jobs = session.scalars(
        select(Job)
        .where(*filters)
        .options(selectinload(Job.application))
        .order_by(Job.match_score.desc(), Job.date_discovered.desc(), Job.id.desc())
        .offset((max(1, page) - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(jobs), total


def get_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None or job.is_demo:
        raise ValueError("Nie znaleziono oferty")
    return job


def job_action(session: Session, job_id: int, action: str, *, today: date | None = None) -> Job:
    job = get_job(session, job_id)
    if action == "save":
        if not job.application:
            change_status(session, job, "Saved", today=today)
    elif action == "apply":
        if job.application and job.application.status not in {"Saved", "Preparing"}:
            return job
        if job.ignored or not job.is_active:
            raise ValueError("Do aplikowania wybierz aktywną, widoczną ofertę")
        change_status(session, job, "Applied", today=today)
    elif action in {"ignore", "restore"}:
        job.ignored = action == "ignore"
    else:
        raise ValueError("Nieznana akcja")
    return job


def edit_application(
    session: Session,
    job_id: int,
    *,
    status: str | None = None,
    notes: str | None = None,
    cv_version: str | None = None,
    next_action: str | None = None,
    today: date | None = None,
) -> Job:
    # Validate before creating an application or recording any status change.
    next_date = date.fromisoformat(next_action) if next_action and next_action != "-" else None
    if status is not None and status not in APPLICATION_STATUSES:
        raise ValueError("Nieprawidłowy status aplikacji")
    job = get_job(session, job_id)
    application = change_status(
        session,
        job,
        status or (job.application.status if job.application else "Saved"),
        today=today,
    )
    update_details(
        session,
        application,
        notes=application.notes if notes is None else notes,
        cv_version=application.cv_version if cv_version is None else cv_version,
        next_action_date=application.next_action_date if next_action is None else next_date,
    )
    return job


def follow_ups(session: Session, today: date) -> list[Application]:
    return list(
        session.scalars(
            select(Application)
            .join(Job)
            .where(
                Job.is_demo.is_(False),
                Application.next_action_date <= today,
                Application.status.not_in(CLOSED_STATUSES),
            )
            .options(selectinload(Application.job))
            .order_by(Application.next_action_date, Application.id)
        )
    )


def add_manual_job(session: Session, text: str) -> Job:
    parts = [part.strip() for part in text.split("|", 4)]
    if len(parts) < 3:
        raise ValueError("Użyj /add Tytuł stażu | Firma | https://link | Hybrid | Opis")
    title, company, url = parts[:3]
    if not title or not company or not is_internship_role(title):
        raise ValueError("Podaj nazwę firmy i tytuł wskazujący staż lub praktyki")
    canonicalize_url(url)
    remote = parts[3] if len(parts) > 3 else "On-site"
    if remote not in {"Hybrid", "On-site"}:
        raise ValueError("Tryb pracy: Hybrid lub On-site")
    job, _ = import_posting(
        session,
        JobPosting(
            source="Manual",
            source_job_id=None,
            title=title,
            company=company,
            url=url,
            location=f"Kraków · {remote}",
            remote_type=remote,
            description=parts[4] if len(parts) > 4 else "",
            raw_payload={"manual": True},
        ),
        session.scalar(select(CandidateProfile).limit(1)),
    )
    return job
