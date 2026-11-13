from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CandidateProfile,
    Job,
    JobOccurrence,
    ProviderRun,
    SavedSearch,
    SearchObservation,
    SearchRun,
    utcnow,
)
from app.profiles.scope import active_scope, job_filter, location_matches, role_matches
from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.common import INTERNSHIP_KEYWORDS
from app.scrapers.registry import enabled_sources
from app.services.backups import automatic_backup
from app.services.deduplication import find_duplicate
from app.services.ignoring import apply_ignore_rules
from app.services.matching import score_job
from app.services.normalization import canonicalize_url, job_hash
from app.services.publication_dates import refresh_posting_dates, save_date_evidence

logger = logging.getLogger(__name__)

DEFAULT_KEYWORDS = [
    "analyst",
    "finance",
    "financial",
    "data analysis",
    "research",
    "economic",
    "fp&a",
    "business analysis",
    "controlling",
    "reporting",
    "risk",
    "credit",
    "investment",
    "treasury",
    "intern",
    "internship",
    "staż",
    "praktyk",
    "analityk",
]


def _new_job(posting: JobPosting, profile: CandidateProfile | None) -> Job:
    canonical_url = canonicalize_url(posting.url)
    job = Job(
        source=posting.source,
        source_job_id=posting.source_job_id,
        canonical_url=canonical_url,
        original_url=posting.url,
        title=posting.title[:300],
        company=posting.company[:200],
        location=posting.location[:240],
        country=posting.country,
        remote_type=posting.remote_type,
        employment_type=posting.employment_type,
        seniority=posting.seniority,
        salary_min=posting.salary_min,
        salary_max=posting.salary_max,
        salary_currency=posting.salary_currency,
        description=posting.description,
        requirements=posting.requirements,
        responsibilities=posting.responsibilities,
        date_posted=posting.date_posted,
        job_hash=job_hash(posting.company, posting.title, posting.location, posting.description),
        raw_payload=posting.raw_payload,
    )
    result = score_job(job, profile)
    job.match_score = result.score
    job.match_explanation = result.explanation
    job.skills = result.skills
    job.missing_skills = result.missing_skills
    job.categories = result.categories
    return job


def _add_occurrence(session: Session, job: Job, posting: JobPosting) -> bool:
    occurrence_filters = [
        JobOccurrence.job_id == job.id,
        JobOccurrence.source == posting.source,
    ]
    if posting.source_job_id:
        occurrence_filters.append(JobOccurrence.source_job_id == posting.source_job_id)
    else:
        occurrence_filters.append(JobOccurrence.url == posting.url)
    occurrence = session.scalar(select(JobOccurrence).where(*occurrence_filters))
    if occurrence:
        occurrence.last_seen = utcnow()
        occurrence.url = posting.url
        occurrence.raw_payload = posting.raw_payload
        return False
    session.add(
        JobOccurrence(
            job=job,
            source=posting.source,
            source_job_id=posting.source_job_id,
            url=posting.url,
            raw_payload=posting.raw_payload,
        )
    )
    return True


def import_posting(
    session: Session, posting: JobPosting, profile: CandidateProfile | None = None
) -> tuple[Job, str]:
    duplicate = find_duplicate(session, posting)
    if duplicate:
        duplicate.last_seen = utcnow()
        duplicate.is_active = True
        if posting.date_posted is not None:
            duplicate.date_posted = (
                min(duplicate.date_posted, posting.date_posted)
                if duplicate.date_posted
                else posting.date_posted
            )
        if evidence := posting.raw_payload.get("_publication_check"):
            save_date_evidence(duplicate, evidence)
        if len(posting.description) > len(duplicate.description):
            duplicate.description = posting.description
        _add_occurrence(session, duplicate, posting)
        result = score_job(duplicate, profile)
        duplicate.match_score = result.score
        duplicate.match_explanation = result.explanation
        duplicate.skills = result.skills
        duplicate.missing_skills = result.missing_skills
        duplicate.categories = result.categories
        apply_ignore_rules(session, duplicate)
        return duplicate, "duplicate"
    job = _new_job(posting, profile)
    session.add(job)
    session.flush()
    _add_occurrence(session, job, posting)
    apply_ignore_rules(session, job)
    return job, "new"


def run_search(session: Session, *, source_names: list[str] | None = None) -> SearchRun:
    started = time.monotonic()
    search_run = SearchRun()
    session.add(search_run)
    session.commit()
    profile = session.scalar(select(CandidateProfile).limit(1))
    sources = enabled_sources()
    if source_names:
        sources = [source for source in sources if source.name in source_names]
    configured_searches = session.scalars(
        select(SavedSearch).where(SavedSearch.enabled.is_(True))
    ).all()
    scope = active_scope()
    keywords = sorted(
        {keyword for search in configured_searches for keyword in search.keywords}
    ) or (scope["titles"] if scope else DEFAULT_KEYWORDS)
    criteria = SearchCriteria(
        keywords=sorted(set(keywords) | (set() if scope else set(INTERNSHIP_KEYWORDS))),
        locations=scope["locations"] if scope else ["Kraków", "Cracow"],
        remote_allowed=scope["remote_allowed"] if scope else False,
    )
    negative_terms = {
        term.casefold() for search in configured_searches for term in search.negative_keywords
    }
    session.commit()  # Do not hold a database write transaction during network requests.
    successful: set[str] = set()
    try:

        def fetch(source):  # noqa: ANN001, ANN202
            provider_started = time.monotonic()
            try:
                return (
                    source,
                    source.search_jobs(criteria),
                    None,
                    time.monotonic() - provider_started,
                )
            except Exception as exc:  # provider isolation is intentional
                return source, [], exc, time.monotonic() - provider_started

        with ThreadPoolExecutor(max_workers=max(1, min(6, len(sources)))) as executor:
            futures = [executor.submit(copy_context().run, fetch, source) for source in sources]
            for future in as_completed(futures):
                source, postings, error, duration = future.result()
                provider_run = ProviderRun(
                    search_run=search_run,
                    provider=source.name,
                    status="failed" if error else "success",
                    duration=round(duration, 3),
                    jobs_found=len(postings),
                    error_message=str(error)[:1000] if error else None,
                )
                session.add(provider_run)
                if error:
                    logger.error("Provider %s failed: %s", source.name, error)
                    search_run.errors += 1
                    session.commit()
                    continue
                provider_run.jobs_found = len(postings)
                if getattr(source, "can_confirm_absence", False):
                    successful.add(source.name)
                if getattr(source, "scan_complete", None) is False:
                    provider_run.status = "partial"
                    provider_run.error_message = getattr(source, "last_search_note", None)
                elif not postings and not getattr(source, "scan_complete", False):
                    provider_run.status = "unverified"
                    provider_run.error_message = (
                        "No parseable matches; this monitor cannot confirm that old offers expired"
                    )
                for posting in postings:
                    observation = SearchObservation(
                        search_run_id=search_run.id,
                        provider=source.name,
                        title=posting.title[:300],
                        company=posting.company[:200],
                        outcome="excluded_role",
                        detail="Outside internship scope",
                    )
                    session.add(observation)
                    if posting.raw_payload.get("talent_pool"):
                        observation.outcome = "excluded_talent_pool"
                        observation.detail = (
                            "Employer advertises future opportunities, not a confirmed vacancy"
                        )
                        continue
                    if not role_matches(posting.title, posting.employment_type, posting.seniority):
                        continue
                    if not location_matches(
                        posting.location,
                        posting.description,
                        posting.remote_type == "Remote",
                    ):
                        observation.outcome = "excluded_location"
                        observation.detail = "Outside Kraków or remote-only"
                        continue
                    try:
                        with session.begin_nested():
                            job, outcome = import_posting(session, posting, profile)
                    except Exception as exc:  # isolate a malformed posting, retain run history
                        observation.outcome = "error"
                        observation.detail = str(exc)[:1000]
                        search_run.errors += 1
                        logger.exception("Could not import %s posting", source.name)
                        continue
                    search_run.jobs_found += 1
                    observation.job_id = job.id
                    observation.outcome = outcome
                    observation.detail = "First discovery" if outcome == "new" else "Seen again"
                    posting_text = f"{posting.title} {posting.description}".casefold()
                    if any(term in posting_text for term in negative_terms):
                        job.ignored = True
                    if job.ignored:
                        observation.detail += "; hidden by an ignore rule or negative keyword"
                    if outcome == "new":
                        search_run.new_jobs += 1
                    else:
                        search_run.updated_jobs += 1
                        search_run.duplicates_found += 1
                session.commit()

        # Preserve history. A job becomes expired only after a successful source check and
        # seven days without being seen, and only a complete structured scan may expire it.
        cutoff = utcnow() - timedelta(days=7)
        if successful:
            session.execute(
                Job.__table__.update()
                .where(
                    job_filter(),
                    Job.source.in_(successful),
                    Job.last_seen < cutoff,
                    Job.is_demo.is_(False),
                )
                .values(is_active=False)
            )
        search_run.status = "completed" if not search_run.errors else "completed_with_errors"
    except Exception:
        session.rollback()
        search_run.status = "failed"
        search_run.errors += 1
        logger.exception("Search run failed")
    finally:
        search_run.sources_checked = len(sources)
        search_run.completed_at = utcnow().replace(microsecond=0)
        search_run.duration = round(time.monotonic() - started, 3)
        session.commit()
        try:
            refresh_posting_dates(session)
        except Exception:
            session.rollback()
            logger.exception("Missing-date enrichment failed; search results were already saved")
        automatic_backup(session.get_bind())
    return search_run
