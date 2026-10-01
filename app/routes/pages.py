from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_session
from app.models import (
    Application,
    CandidateProfile,
    CompanyPreference,
    CvVersion,
    IgnoreRule,
    Job,
    SavedSearch,
    SearchRun,
)
from app.scrapers.registry import provider_catalog
from app.services.assistant import ApplicationAssistant

router = APIRouter()

APPLIED_STATUSES = {
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


def _templates(request: Request):  # noqa: ANN202
    return request.app.state.templates


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    latest_run = session.scalar(select(SearchRun).order_by(SearchRun.started_at.desc()).limit(1))
    today_start = datetime.combine(date.today(), datetime.min.time())
    week_start = today_start - timedelta(days=7)
    new_today = (
        session.scalar(
            select(func.count(Job.id)).where(
                Job.date_discovered >= today_start,
                Job.ignored.is_(False),
                ~Job.application.has(Application.status.in_(APPLIED_STATUSES)),
            )
        )
        or 0
    )
    new_week = (
        session.scalar(
            select(func.count(Job.id)).where(
                Job.date_discovered >= week_start,
                Job.ignored.is_(False),
                ~Job.application.has(Application.status.in_(APPLIED_STATUSES)),
            )
        )
        or 0
    )
    active_jobs = (
        session.scalar(
            select(func.count(Job.id)).where(Job.is_active.is_(True), Job.ignored.is_(False))
        )
        or 0
    )
    applications = session.scalars(select(Application)).all()
    interview_statuses = {
        "Recruiter Screen",
        "Technical/Case Interview",
        "Hiring Manager Interview",
        "Final Interview",
    }
    recent_jobs = session.scalars(
        select(Job)
        .where(
            Job.ignored.is_(False),
            Job.is_active.is_(True),
            ~Job.application.has(Application.status.in_(APPLIED_STATUSES)),
        )
        .order_by(Job.date_discovered.desc(), Job.match_score.desc())
        .limit(6)
    ).all()
    matching_jobs = session.scalars(
        select(Job).where(Job.is_active.is_(True), Job.ignored.is_(False), Job.match_score >= 50)
    ).all()
    skill_counts = Counter(skill for job in matching_jobs for skill in job.skills)
    denominator = max(1, len(matching_jobs))
    profile = session.scalar(select(CandidateProfile).limit(1))
    have = {
        item.casefold() for item in ((profile.skills + profile.technologies) if profile else [])
    }
    skills = [
        {
            "today_label": date.today().strftime("%A, %d %B").upper(),
            "name": name,
            "count": count,
            "percent": round(count / denominator * 100),
            "state": "Have" if name.casefold() in have else "Missing",
        }
        for name, count in skill_counts.most_common(8)
    ]
    watched = {
        pref.company.casefold()
        for pref in session.scalars(
            select(CompanyPreference).where(CompanyPreference.watched.is_(True))
        )
    }
    watchlist_jobs = [
        job
        for job in recent_jobs
        if job.company.casefold() in watched and job.date_discovered >= week_start
    ]
    return _templates(request).TemplateResponse(
        request,
        "dashboard.html",
        {
            "latest_run": latest_run,
            "new_today": new_today,
            "new_week": new_week,
            "active_jobs": active_jobs,
            "applications_count": len(applications),
            "interviews": sum(app.status in interview_statuses for app in applications),
            "recent_jobs": recent_jobs,
            "skills": skills,
            "watchlist_jobs": watchlist_jobs,
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    session: Session = Depends(get_session),
    view: str = "all",
    q: str = "",
    company: str = "",
    source: str = "",
    remote_type: str = "",
    employment_type: str = "",
    status: str = "",
    min_score: int = Query(0, ge=0, le=100),
    days: int | None = Query(None, ge=0, le=365),
    hide_applied: str = "",
    sort: str = "score",
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    filters = [Job.ignored.is_(False)]
    selected_days = 7 if days is None and view == "new" else (days or None)
    applied_hidden = hide_applied == "1" or (view == "new" and hide_applied != "0")
    if view == "new":
        filters.append(Job.is_active.is_(True))
    elif view == "saved":
        filters.append(Job.application.has(Application.status == "Saved"))
    elif view == "ignored":
        filters = [Job.ignored.is_(True)]
    if selected_days:
        filters.append(Job.date_discovered >= datetime.now() - timedelta(days=selected_days))
    if applied_hidden:
        filters.append(~Job.application.has(Application.status.in_(APPLIED_STATUSES)))
    if q:
        pattern = f"%{q}%"
        filters.append(
            or_(Job.title.ilike(pattern), Job.description.ilike(pattern), Job.skills.contains(q))
        )
    if company:
        filters.append(Job.company == company)
    if source:
        filters.append(Job.source == source)
    if remote_type:
        filters.append(Job.remote_type == remote_type)
    if employment_type:
        filters.append(Job.employment_type == employment_type)
    if status:
        filters.append(Job.application.has(Application.status == status))
    if min_score:
        filters.append(Job.match_score >= min_score)
    order = {
        "score": Job.match_score.desc(),
        "newest": Job.date_discovered.desc(),
        "posted": Job.date_posted.desc(),
        "company": Job.company.asc(),
    }.get(sort, Job.match_score.desc())
    base = select(Job).where(*filters)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    per_page = 12
    jobs = session.scalars(
        base.options(selectinload(Job.application))
        .order_by(order, Job.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    filter_data = {
        "companies": session.scalars(select(Job.company).distinct().order_by(Job.company)).all(),
        "sources": session.scalars(select(Job.source).distinct().order_by(Job.source)).all(),
        "employment_types": session.scalars(
            select(Job.employment_type).distinct().order_by(Job.employment_type)
        ).all(),
    }
    return _templates(request).TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "view": view,
            "total": total,
            "page": page,
            "pages": max(1, math.ceil(total / per_page)),
            "filters": filter_data,
            "query": request.query_params,
            "selected_days": selected_days,
            "applied_hidden": applied_hidden,
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: int, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    job = session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .options(
            selectinload(Job.occurrences),
            selectinload(Job.application).selectinload(Application.activities),
        )
    )
    if job is None:
        return _templates(request).TemplateResponse(request, "404.html", status_code=404)
    profile = session.scalar(select(CandidateProfile).limit(1))
    analysis = ApplicationAssistant().analyze_job(job, profile)
    cv_versions = session.scalars(
        select(CvVersion).where(CvVersion.active.is_(True)).order_by(CvVersion.name)
    ).all()
    return _templates(request).TemplateResponse(
        request, "job_detail.html", {"job": job, "analysis": analysis, "cv_versions": cv_versions}
    )


@router.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    applications = session.scalars(
        select(Application)
        .options(selectinload(Application.job))
        .order_by(Application.last_activity.desc())
    ).all()
    columns = {
        "Saved": ["Saved"],
        "Preparing": ["Preparing"],
        "Applied": ["Applied"],
        "Interview": [
            "Recruiter Screen",
            "Technical/Case Interview",
            "Hiring Manager Interview",
            "Final Interview",
        ],
        "Offer": ["Offer"],
        "Rejected": ["Rejected", "Withdrawn", "Expired"],
    }
    grouped = {
        label: [app for app in applications if app.status in statuses]
        for label, statuses in columns.items()
    }
    return _templates(request).TemplateResponse(
        request,
        "applications.html",
        {
            "groups": grouped,
            "today": date.today(),
            "all_statuses": [s for v in columns.values() for s in v],
        },
    )


@router.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    jobs = session.scalars(select(Job).options(selectinload(Job.application))).all()
    prefs = {p.company.casefold(): p for p in session.scalars(select(CompanyPreference)).all()}
    companies: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "jobs": 0,
            "active": 0,
            "saved": 0,
            "applications": 0,
            "interviews": 0,
            "offers": 0,
            "rejected": 0,
            "last_new": None,
        }
    )
    for job in jobs:
        row = companies[job.company]
        row["jobs"] += 1  # type: ignore[operator]
        row["active"] += int(job.is_active)  # type: ignore[operator]
        last_new = row["last_new"]
        if last_new is None or job.date_discovered > last_new:
            row["last_new"] = job.date_discovered
        if job.application:
            row["applications"] += 1  # type: ignore[operator]
            row["saved"] += int(job.application.status == "Saved")  # type: ignore[operator]
            row["interviews"] += int(
                "Interview" in job.application.status
                or job.application.status == "Recruiter Screen"
            )  # type: ignore[operator]
            row["offers"] += int(job.application.status == "Offer")  # type: ignore[operator]
            row["rejected"] += int(job.application.status == "Rejected")  # type: ignore[operator]
    rows = [
        {"name": name, **stats, "preference": prefs.get(name.casefold())}
        for name, stats in sorted(companies.items())
    ]
    for pref in prefs.values():
        if pref.company not in companies:
            rows.append(
                {
                    "name": pref.company,
                    "jobs": 0,
                    "active": 0,
                    "saved": 0,
                    "applications": 0,
                    "interviews": 0,
                    "offers": 0,
                    "rejected": 0,
                    "last_new": None,
                    "preference": pref,
                }
            )
    return _templates(request).TemplateResponse(request, "companies.html", {"companies": rows})


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    runs = session.scalars(
        select(SearchRun)
        .options(selectinload(SearchRun.provider_runs))
        .order_by(SearchRun.started_at.desc())
    ).all()
    return _templates(request).TemplateResponse(request, "runs.html", {"runs": runs})


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    profile = session.scalar(select(CandidateProfile).limit(1))
    cvs = session.scalars(select(CvVersion).order_by(CvVersion.name)).all()
    return _templates(request).TemplateResponse(
        request, "profile.html", {"profile": profile, "cvs": cvs}
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    latest = session.scalar(
        select(SearchRun)
        .options(selectinload(SearchRun.provider_runs))
        .order_by(SearchRun.started_at.desc())
        .limit(1)
    )
    searches = session.scalars(select(SavedSearch).order_by(SavedSearch.name)).all()
    ignore_rules = session.scalars(select(IgnoreRule).order_by(IgnoreRule.created_at.desc())).all()
    return _templates(request).TemplateResponse(
        request,
        "settings.html",
        {
            "providers": provider_catalog(),
            "latest_run": latest,
            "searches": searches,
            "ignore_rules": ignore_rules,
            "excel_path": settings.excel_jobs_path,
            "excel_exists": bool(
                settings.excel_jobs_path and Path(settings.excel_jobs_path).is_file()
            ),
        },
    )


@router.get("/jobs/new/manual", response_class=HTMLResponse)
def manual_job_page(request: Request) -> HTMLResponse:
    return _templates(request).TemplateResponse(request, "manual_job.html")
