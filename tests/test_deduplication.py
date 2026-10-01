from __future__ import annotations

from app.scrapers.base import JobPosting
from app.services.deduplication import postings_match
from app.services.search import import_posting


def posting(title: str, url: str = "https://jobs.example/one", source: str = "A") -> JobPosting:
    return JobPosting(
        source=source,
        source_job_id=None,
        url=url,
        title=title,
        company="Acme Sp. z o.o.",
        location="Kraków, Poland",
        description="Analyze financial data, prepare monthly reporting and support budgeting. " * 3,
    )


def test_finance_intern_title_variants_are_duplicates() -> None:
    assert postings_match(
        posting("Financial Analyst Intern"),
        posting("Finance Analyst Internship", "https://other.example/42", "B"),
    )


def test_finance_and_risk_roles_remain_distinct() -> None:
    assert not postings_match(
        posting("Finance Analyst Intern"),
        posting("Risk Analyst Intern", "https://other.example/42", "B"),
    )


def test_source_id_wins() -> None:
    left = posting("Analyst Intern")
    right = posting("Completely renamed position")
    left.source_job_id = right.source_job_id = "123"
    assert postings_match(left, right)


def test_import_preserves_second_source_as_occurrence(session) -> None:  # noqa: ANN001
    first, outcome_one = import_posting(session, posting("Financial Analyst Intern"))
    session.commit()
    second, outcome_two = import_posting(
        session,
        posting("Finance Analyst Internship", "https://other.example/42", "B"),
    )
    session.commit()
    assert outcome_one == "new"
    assert outcome_two == "duplicate"
    assert second.id == first.id
    assert len(second.occurrences) == 2


def test_manual_jobs_without_source_ids_do_not_all_merge(session) -> None:  # noqa: ANN001
    first = posting("Finance Analyst Intern", "https://jobs.example/finance", "Manual")
    second = posting("Risk Analyst Intern", "https://jobs.example/risk", "Manual")
    imported_first, _ = import_posting(session, first)
    session.commit()
    imported_second, outcome = import_posting(session, second)
    session.commit()
    assert outcome == "new"
    assert imported_first.id != imported_second.id
