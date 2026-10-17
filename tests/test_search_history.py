from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProviderRun, SearchObservation, SearchRun
from app.scrapers.base import JobPosting
from app.services import search


def test_search_preserves_decisions_and_isolates_bad_offer(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        name = "Audited"

        def search_jobs(self, criteria):  # noqa: ANN001, ANN202
            # Run is committed before network work starts.
            with Session(session.get_bind()) as observer:
                assert observer.scalar(select(SearchRun.id)) is not None
            return [
                JobPosting(
                    source=self.name,
                    source_job_id=str(i),
                    title=title,
                    company=f"Firm {i}",
                    location=location,
                    url=url,
                )
                for i, (title, location, url) in enumerate(
                    [
                        ("Finance Analyst", "Kraków", "https://example.com/regular"),
                        ("Finance Intern", "Warsaw", "https://example.com/warsaw"),
                        ("Finance Intern", "Kraków", "javascript:invalid"),
                        ("Finance Intern", "Kraków", "https://example.com/good"),
                    ]
                )
            ]

    monkeypatch.setattr(search, "enabled_sources", lambda: [Provider()])
    first = search.run_search(session)
    assert first.status == "completed_with_errors"
    assert (first.new_jobs, first.errors) == (1, 1)
    observations = session.scalars(
        select(SearchObservation)
        .where(SearchObservation.search_run_id == first.id)
        .order_by(SearchObservation.id)
    ).all()
    assert [item.outcome for item in observations] == [
        "excluded_role",
        "excluded_location",
        "error",
        "new",
    ]
    assert observations[-1].job_id is not None
    second = search.run_search(session)
    assert second.new_jobs == 0 and second.duplicates_found == 1
    assert (
        session.scalar(
            select(SearchObservation.outcome).where(
                SearchObservation.search_run_id == second.id, SearchObservation.job_id.is_not(None)
            )
        )
        == "duplicate"
    )


def test_empty_monitor_is_recorded_as_unverified(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Monitor:
        name = "Dynamic careers page"

        def search_jobs(self, criteria):  # noqa: ANN001, ANN202
            return []

    monkeypatch.setattr(search, "enabled_sources", lambda: [Monitor()])
    search.run_search(session)
    provider = session.scalar(select(ProviderRun))
    assert provider.status == "unverified"
    assert "cannot confirm" in provider.error_message
