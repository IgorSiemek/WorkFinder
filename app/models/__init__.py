from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(255), index=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    original_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300), index=True)
    company: Mapped[str] = mapped_column(String(200), index=True)
    location: Mapped[str] = mapped_column(String(240), index=True, default="")
    country: Mapped[str] = mapped_column(String(80), default="Poland")
    remote_type: Mapped[str] = mapped_column(String(40), default="On-site", index=True)
    employment_type: Mapped[str] = mapped_column(String(80), default="Not specified")
    seniority: Mapped[str] = mapped_column(String(80), default="Not specified")
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(Text, default="")
    requirements: Mapped[str] = mapped_column(Text, default="")
    responsibilities: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    date_posted: Mapped[date | None] = mapped_column(Date)
    date_discovered: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    application_deadline: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    match_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    match_explanation: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    missing_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    job_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    occurrences: Mapped[list[JobOccurrence]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    application: Mapped[Application | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )


class JobOccurrence(Base):
    __tablename__ = "job_occurrences"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="occurrences")


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), default="running")
    sources_checked: Mapped[int] = mapped_column(Integer, default=0)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    updated_jobs: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_found: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float | None] = mapped_column(Float)

    provider_runs: Mapped[list[ProviderRun]] = relationship(
        back_populates="search_run", cascade="all, delete-orphan"
    )


class ProviderRun(Base):
    __tablename__ = "provider_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    search_run_id: Mapped[int] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    search_run: Mapped[SearchRun] = relationship(back_populates="provider_runs")


class Application(Base, TimestampMixin):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(60), default="Saved", index=True)
    date_saved: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    date_started: Mapped[datetime | None] = mapped_column(DateTime)
    date_applied: Mapped[datetime | None] = mapped_column(DateTime)
    last_activity: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    next_action_date: Mapped[date | None] = mapped_column(Date)
    application_url: Mapped[str | None] = mapped_column(Text)
    cv_version: Mapped[str | None] = mapped_column(String(160))
    cover_letter_version: Mapped[str | None] = mapped_column(String(160))
    contact_person: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    notes: Mapped[str] = mapped_column(Text, default="")
    salary_expectation: Mapped[str | None] = mapped_column(String(120))
    interview_date: Mapped[datetime | None] = mapped_column(DateTime)
    rejection_date: Mapped[date | None] = mapped_column(Date)
    offer_date: Mapped[date | None] = mapped_column(Date)

    job: Mapped[Job] = relationship(back_populates="application")
    activities: Mapped[list[ApplicationActivity]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationActivity.at",
    )


class ApplicationActivity(Base):
    __tablename__ = "application_activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    event_type: Mapped[str] = mapped_column(String(60), default="note")
    description: Mapped[str] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="activities")


class CandidateProfile(Base, TimestampMixin):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="My profile")
    education: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    technologies: Mapped[list[str]] = mapped_column(JSON, default=list)
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_jobs: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    available_hours: Mapped[str] = mapped_column(String(120), default="Full-time")
    desired_salary: Mapped[str] = mapped_column(String(120), default="")
    remote_preference: Mapped[str] = mapped_column(String(80), default="Hybrid")


class CvVersion(Base, TimestampMixin):
    __tablename__ = "cv_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CompanyPreference(Base, TimestampMixin):
    __tablename__ = "company_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    priority: Mapped[str] = mapped_column(String(30), default="Normal")
    watched: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class SavedSearch(Base, TimestampMixin):
    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    negative_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    minimum_score: Mapped[int] = mapped_column(Integer, default=50)
    seniority: Mapped[list[str]] = mapped_column(JSON, default=list)
    employment_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    companies: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class IgnoreRule(Base, TimestampMixin):
    __tablename__ = "ignore_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_type: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
