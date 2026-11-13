from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Job
from app.profiles.scope import job_filter
from app.scrapers.publication_dates import lookup_publication_date, source_date
from app.services.backups import automatic_backup


def save_date_evidence(job: Job, evidence: dict) -> None:
    payload = dict(job.raw_payload or {})
    previous = payload.get("_publication_check")
    history = list(payload.get("_publication_history", []))
    if previous and any(
        previous.get(key) != evidence.get(key)
        for key in (
            "status",
            "date",
            "estimated_date",
            "source_url",
            "method",
        )
    ):
        history.append(previous)
    payload["_publication_check"] = evidence
    if history:
        payload["_publication_history"] = history
    job.raw_payload = payload
    posted = source_date(evidence.get("date"))
    if posted and evidence.get("status") == "verified":
        # Keep the earliest verified date if the employer later republishes the same role.
        job.date_posted = min(job.date_posted, posted) if job.date_posted else posted


def refresh_posting_dates(
    session: Session,
    *,
    job_id: int | None = None,
    limit: int = 100,
    force: bool = False,
) -> dict[str, int]:
    today = datetime.now(UTC).date().isoformat()
    query = select(Job).where(Job.is_demo.is_(False))
    if job_id is not None:
        query = query.where(Job.id == job_id)
    else:
        query = query.where(Job.date_posted.is_(None), job_filter())
    if not force:
        query = query.where(
            func.coalesce(Job.raw_payload["_publication_check"]["checked_on"].as_string(), "")
            != today
        )
    jobs = session.scalars(
        query.options(selectinload(Job.occurrences))
        .order_by(
            Job.raw_payload["_publication_check"]["checked_at"].as_string().asc().nulls_first(),
            Job.id,
        )
        .limit(limit)
    ).all()
    targets = []
    for job in jobs:
        # Try provenance links too: an Excel entry can point to an outdated original URL.
        urls = list(dict.fromkeys([job.original_url, *(item.url for item in job.occurrences)]))
        targets.append((job.id, job.title, urls[:3]))
    session.commit()

    def fetch(title: str, urls: list[str]) -> dict:
        best = None
        for url in urls:
            result, evidence = lookup_publication_date(url, title)
            if result.value:
                return evidence
            if best is None or evidence["status"] == "relative":
                best = evidence
        return best

    counts = {"verified": 0, "relative": 0, "unknown": 0}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch, title, urls): identifier for identifier, title, urls in targets
        }
        for future in as_completed(futures):
            evidence = future.result()
            job = session.get(Job, futures[future])
            if job is None:
                continue
            save_date_evidence(job, evidence)
            status = evidence["status"]
            counts[status if status in {"verified", "relative"} else "unknown"] += 1
            session.commit()
    automatic_backup(session.get_bind())
    return counts
