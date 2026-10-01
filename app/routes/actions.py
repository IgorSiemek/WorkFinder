from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_session
from app.models import (
    Application,
    ApplicationActivity,
    CandidateProfile,
    CompanyPreference,
    CvVersion,
    IgnoreRule,
    Job,
    SavedSearch,
    utcnow,
)
from app.scrapers.base import JobPosting
from app.scrapers.common import is_senior_role
from app.services.excel_import import import_excel_jobs
from app.services.excel_sync import mark_applied_in_excel
from app.services.ignoring import recalculate_rule_ignores
from app.services.matching import score_job
from app.services.normalization import canonicalize_url
from app.services.search import import_posting, run_search
from app.services.seed import remove_demo, seed_demo

router = APIRouter(prefix="/actions")


def redirect(path: str, message: str = "Updated") -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}message={message}", status_code=303)


def _safe_return_path(value: str, fallback: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else fallback


def _excel_error(exc: OSError | ValueError) -> str:
    if isinstance(exc, PermissionError):
        return "Close the Excel workbook and try again"
    return str(exc)


@router.post("/search")
def search_now(session: Session = Depends(get_session)) -> RedirectResponse:
    result = run_search(session)
    return redirect("/", f"Search complete: {result.new_jobs} new jobs")


@router.post("/excel/import")
def excel_import(session: Session = Depends(get_session)) -> RedirectResponse:
    if not settings.excel_jobs_path:
        return redirect("/settings", "Set EXCEL_JOBS_PATH in .env first")
    try:
        result = import_excel_jobs(session, settings.excel_jobs_path)
    except (FileNotFoundError, ValueError) as exc:
        return redirect("/settings", str(exc))
    return redirect(
        "/settings",
        (
            f"Excel import: {result.new_jobs} new, {result.updated_jobs} updated, "
            f"{result.applications_created} applications, {result.skipped_rows} skipped"
        ),
    )


@router.post("/jobs/{job_id}/save")
def save_job(job_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    job = session.get(Job, job_id)
    if job and not job.application:
        app = Application(job=job, status="Saved")
        session.add(app)
        session.flush()
        session.add(ApplicationActivity(application=app, event_type="status", description="Saved"))
        session.commit()
    return redirect(f"/jobs/{job_id}", "Job saved")


@router.post("/jobs/{job_id}/mark-applied")
def mark_job_applied(
    job_id: int,
    return_to: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    target = _safe_return_path(return_to, f"/jobs/{job_id}")
    job = session.get(Job, job_id)
    if job is None:
        return redirect("/jobs", "Job not found")
    if job.is_demo or job.ignored or not job.is_active:
        return redirect(target, "Only active Kraków jobs can be marked as applied")
    if job.application and job.application.status not in {"Saved", "Preparing"}:
        return redirect(target, f"Job is already {job.application.status}")
    if not settings.excel_jobs_path:
        return redirect(target, "Set EXCEL_JOBS_PATH in .env first")

    applied_at = utcnow()
    try:
        excel_result = mark_applied_in_excel(
            settings.excel_jobs_path, job, applied_at.date()
        )
    except (OSError, ValueError) as exc:
        return redirect(target, _excel_error(exc))

    application = job.application or Application(job=job, status="Applied")
    if application.id is None:
        session.add(application)
        session.flush()
    application.status = "Applied"
    application.date_applied = applied_at
    application.last_activity = applied_at
    application.application_url = job.original_url
    session.add(
        ApplicationActivity(
            application=application,
            event_type="status",
            description=(
                "Marked as Applied; added to Excel"
                if excel_result.created
                else "Marked as Applied; Excel row updated"
            ),
        )
    )
    session.commit()
    action = "added to" if excel_result.created else "updated in"
    return redirect(
        target,
        f"Marked Applied and {action} Excel row {excel_result.row_number}",
    )


@router.post("/jobs/{job_id}/ignore")
def ignore_job(
    job_id: int,
    return_to: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    target = _safe_return_path(return_to, f"/jobs/{job_id}")
    job = session.get(Job, job_id)
    if job:
        job.ignored = not job.ignored
        session.commit()
    return redirect(target, "Job visibility updated")


@router.post("/jobs/{job_id}/application")
def update_application(
    job_id: int,
    status: str = Form(...),
    notes: str = Form(""),
    next_action_date: str = Form(""),
    cv_version: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    allowed = {
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
    }
    if status not in allowed:
        return redirect(f"/jobs/{job_id}", "Invalid status")
    job = session.get(Job, job_id)
    if job is None:
        return redirect("/jobs", "Job not found")
    application = job.application or Application(job=job, status="Saved")
    if application.id is None:
        session.add(application)
        session.flush()
    old_status = application.status
    if status == "Applied" and old_status != "Applied":
        if not settings.excel_jobs_path:
            session.rollback()
            return redirect(f"/jobs/{job_id}", "Set EXCEL_JOBS_PATH in .env first")
        try:
            mark_applied_in_excel(settings.excel_jobs_path, job, date.today())
        except (OSError, ValueError) as exc:
            session.rollback()
            return redirect(f"/jobs/{job_id}", _excel_error(exc))
    application.status = status
    application.notes = notes
    application.cv_version = cv_version or None
    application.next_action_date = (
        date.fromisoformat(next_action_date) if next_action_date else None
    )
    application.last_activity = utcnow()
    if status == "Applied" and application.date_applied is None:
        application.date_applied = utcnow()
    if status == "Offer":
        application.offer_date = date.today()
    if status == "Rejected":
        application.rejection_date = date.today()
    if old_status != status:
        session.add(
            ApplicationActivity(
                application=application,
                event_type="status",
                description=f"Status changed from {old_status} to {status}",
            )
        )
    if notes:
        session.add(
            ApplicationActivity(application=application, event_type="note", description=notes)
        )
    session.commit()
    return redirect(f"/jobs/{job_id}", "Application updated")


@router.post("/applications/{application_id}/status")
def kanban_status(
    application_id: int,
    status: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    application = session.get(Application, application_id)
    if application:
        old = application.status
        if status == "Applied" and old != "Applied":
            if not settings.excel_jobs_path:
                return redirect("/applications", "Set EXCEL_JOBS_PATH in .env first")
            try:
                mark_applied_in_excel(
                    settings.excel_jobs_path, application.job, date.today()
                )
            except (OSError, ValueError) as exc:
                return redirect("/applications", _excel_error(exc))
        application.status = status
        application.last_activity = utcnow()
        if status == "Applied" and application.date_applied is None:
            application.date_applied = utcnow()
        session.add(
            ApplicationActivity(
                application=application,
                event_type="status",
                description=f"Status changed from {old} to {status}",
            )
        )
        session.commit()
    return redirect("/applications", "Status updated")


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@router.post("/profile")
def update_profile(
    name: str = Form(...),
    education: str = Form(""),
    skills: str = Form(""),
    technologies: str = Form(""),
    languages: str = Form(""),
    preferred_jobs: str = Form(""),
    preferred_industries: str = Form(""),
    available_hours: str = Form("Full-time"),
    desired_salary: str = Form(""),
    remote_preference: str = Form("Hybrid"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    profile = session.scalar(select(CandidateProfile).limit(1)) or CandidateProfile()
    session.add(profile)
    profile.name = name
    profile.education = education
    profile.skills = _csv(skills)
    profile.technologies = _csv(technologies)
    profile.languages = _csv(languages)
    profile.preferred_jobs = _csv(preferred_jobs)
    profile.preferred_industries = _csv(preferred_industries)
    profile.preferred_locations = ["Kraków"]
    profile.available_hours = available_hours
    profile.desired_salary = desired_salary
    profile.remote_preference = remote_preference
    for job in session.scalars(select(Job)).all():
        result = score_job(job, profile)
        job.match_score = result.score
        job.match_explanation = result.explanation
        job.skills = result.skills
        job.missing_skills = result.missing_skills
        job.categories = result.categories
    session.commit()
    return redirect("/profile", "Profile and scores updated")


@router.post("/cv")
def add_cv(
    name: str = Form(...), notes: str = Form(""), session: Session = Depends(get_session)
) -> RedirectResponse:
    if name.strip() and not session.scalar(select(CvVersion).where(CvVersion.name == name.strip())):
        session.add(CvVersion(name=name.strip(), notes=notes))
        session.commit()
    return redirect("/profile", "CV version added")


@router.post("/companies/{company}/priority")
def company_priority(
    company: str, priority: str = Form(...), session: Session = Depends(get_session)
) -> RedirectResponse:
    pref = session.scalar(select(CompanyPreference).where(CompanyPreference.company == company))
    if pref is None:
        pref = CompanyPreference(company=company)
        session.add(pref)
    pref.priority = priority
    pref.watched = priority != "Ignored"
    if priority == "Ignored":
        for job in session.scalars(select(Job).where(Job.company == company)).all():
            job.ignored = True
    session.commit()
    return redirect("/companies", "Company preference updated")


@router.post("/saved-search")
def add_saved_search(
    name: str = Form(...),
    keywords: str = Form(...),
    negative_keywords: str = Form(""),
    minimum_score: int = Form(50),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if name.strip() and not session.scalar(
        select(SavedSearch).where(SavedSearch.name == name.strip())
    ):
        session.add(
            SavedSearch(
                name=name.strip(),
                keywords=_csv(keywords),
                negative_keywords=_csv(negative_keywords),
                locations=["Kraków"],
                minimum_score=min(100, max(0, minimum_score)),
            )
        )
        session.commit()
    return redirect("/settings", "Saved search added")


@router.post("/saved-search/{search_id}/toggle")
def toggle_saved_search(
    search_id: int, session: Session = Depends(get_session)
) -> RedirectResponse:
    saved_search = session.get(SavedSearch, search_id)
    if saved_search:
        saved_search.enabled = not saved_search.enabled
        session.commit()
    return redirect("/settings", "Saved search updated")


@router.post("/ignore-rule")
def add_ignore_rule(
    rule_type: str = Form(...),
    value: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if rule_type in {"keyword", "company"} and value.strip():
        session.add(IgnoreRule(rule_type=rule_type, value=value.strip()))
        session.flush()
        recalculate_rule_ignores(session)
        session.commit()
    return redirect("/settings", "Ignore rule added")


@router.post("/manual-job")
def manual_job(
    title: str = Form(...),
    company: str = Form(...),
    url: str = Form(...),
    remote_type: str = Form("On-site"),
    employment_type: str = Form("Not specified"),
    description: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if is_senior_role(title):
        return redirect("/jobs/new/manual", "Senior-level roles are excluded")
    if remote_type not in {"On-site", "Hybrid"}:
        remote_type = "On-site"
    try:
        canonicalize_url(url)
    except ValueError:
        return redirect("/jobs/new/manual", "Enter a valid http(s) URL")
    profile = session.scalar(select(CandidateProfile).limit(1))
    job, outcome = import_posting(
        session,
        JobPosting(
            source="Manual",
            source_job_id=None,
            url=url,
            title=title,
            company=company,
            location=f"Kraków · {remote_type}",
            remote_type=remote_type,
            employment_type=employment_type,
            description=description,
            raw_payload={"manual": True},
        ),
        profile,
    )
    session.commit()
    return redirect(f"/jobs/{job.id}", f"Manual job {outcome}")


@router.post("/demo/seed")
def demo_seed(session: Session = Depends(get_session)) -> RedirectResponse:
    count = seed_demo(session)
    return redirect("/settings", f"Added {count} demo jobs")


@router.post("/demo/remove")
def demo_remove(session: Session = Depends(get_session)) -> RedirectResponse:
    count = remove_demo(session)
    return redirect("/settings", f"Removed {count} demo jobs")
