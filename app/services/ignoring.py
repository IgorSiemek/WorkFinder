from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IgnoreRule, Job


def matches_ignore_rule(job: Job, rule: IgnoreRule) -> bool:
    value = rule.value.casefold().strip()
    if not value:
        return False
    if rule.rule_type == "company":
        return value in job.company.casefold()
    haystack = " ".join([job.title, job.description, job.requirements]).casefold()
    return value in haystack


def apply_ignore_rules(session: Session, job: Job) -> bool:
    rules = session.scalars(select(IgnoreRule).where(IgnoreRule.active.is_(True))).all()
    ignored = any(matches_ignore_rule(job, rule) for rule in rules)
    if ignored:
        job.ignored = True
    return ignored


def recalculate_rule_ignores(session: Session) -> None:
    rules = session.scalars(select(IgnoreRule).where(IgnoreRule.active.is_(True))).all()
    for job in session.scalars(select(Job)).all():
        if any(matches_ignore_rule(job, rule) for rule in rules):
            job.ignored = True
