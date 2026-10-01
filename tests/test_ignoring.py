from __future__ import annotations

from app.models import IgnoreRule, Job
from app.services.ignoring import apply_ignore_rules


def test_keyword_rule_hides_but_keeps_job(session) -> None:  # noqa: ANN001
    job = Job(
        source="Test",
        canonical_url="https://example.com/sales",
        original_url="https://example.com/sales",
        title="Sales Analyst",
        company="Example",
        description="Commercial sales reporting",
        job_hash="hash",
    )
    session.add_all([job, IgnoreRule(rule_type="keyword", value="sales")])
    session.flush()
    assert apply_ignore_rules(session, job)
    assert job.ignored
    assert session.get(Job, job.id) is job
