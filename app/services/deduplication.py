from __future__ import annotations

from difflib import SequenceMatcher

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Job, JobOccurrence
from app.scrapers.base import JobPosting
from app.services.normalization import (
    canonicalize_url,
    description_fingerprint,
    normalize_company,
    normalize_location,
    normalize_title,
)


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def postings_match(left: JobPosting | Job, right: JobPosting | Job) -> bool:
    left_source_id = getattr(left, "source_job_id", None)
    right_source_id = getattr(right, "source_job_id", None)
    if left.source == right.source and left_source_id and left_source_id == right_source_id:
        return True
    try:
        left_url = canonicalize_url(getattr(left, "url", None) or left.canonical_url)
        right_url = canonicalize_url(getattr(right, "url", None) or right.canonical_url)
        if left_url == right_url:
            return True
    except ValueError:
        pass
    if normalize_company(left.company) != normalize_company(right.company):
        return False
    if normalize_location(left.location) != normalize_location(right.location):
        return False
    left_title = normalize_title(left.title)
    right_title = normalize_title(right.title)
    generic_title_words = {
        "analyst",
        "intern",
        "junior",
        "entry",
        "level",
        "graduate",
        "assistant",
    }
    left_focus = set(left_title.split()) - generic_title_words
    right_focus = set(right_title.split()) - generic_title_words
    if left_focus and right_focus and left_focus.isdisjoint(right_focus):
        return False
    title_score = similarity(left_title, right_title)
    left_desc = description_fingerprint(left.description)
    right_desc = description_fingerprint(right.description)
    if left_desc == right_desc and len(left.description) > 80:
        return title_score >= 0.82
    return title_score >= 0.84


def find_duplicate(session: Session, posting: JobPosting) -> Job | None:
    direct_conditions = [Job.canonical_url == canonicalize_url(posting.url)]
    if posting.source_job_id:
        direct_conditions.extend(
            [
                (Job.source == posting.source) & (Job.source_job_id == posting.source_job_id),
                (JobOccurrence.source == posting.source)
                & (JobOccurrence.source_job_id == posting.source_job_id),
            ]
        )
    direct = session.scalar(
        select(Job).outerjoin(JobOccurrence).where(or_(*direct_conditions)).limit(1)
    )
    if direct:
        return direct
    company_token = posting.company.split()[0][:40]
    candidates = session.scalars(select(Job).where(Job.company.ilike(f"%{company_token}%"))).all()
    return next((job for job in candidates if postings_match(job, posting)), None)
