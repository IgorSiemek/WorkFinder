from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Job, ProviderRun, SearchObservation, SearchRun
from app.scrapers.base import JobPosting
from app.scrapers.publication_dates import lookup_publication_date
from app.scrapers.workday import WorkdayConfig, WorkdaySource
from app.scrapers.workday_limits import WorkdayRateLimited
from app.services import search
from app.services.bot_config import validate_config
from app.telegram.controller import BotController


@pytest.mark.parametrize(
    "future_description",
    [
        "This exact role may not be open today.",
        "This role is part of our talent pipeline, which means it is not currently open.",
        "A future opportunity. Click Apply to express your interest and be considered "
        "for future opportunities that match your experience and aspirations.",
    ],
)
def test_workday_talent_pools_are_audited_while_real_internships_are_imported(
    session, monkeypatch, future_description
):
    source = WorkdaySource(WorkdayConfig("ABB Careers", "ABB", "example.com", "abb", "External"))
    records = [
        {"title": title, "locationsText": "Krakow", "externalPath": f"/job/Krakow/Intern_R{i}"}
        for i, title in enumerate(
            ["Student Internship Talent Pool", "Risk Analyst Intern", "BI Analyst Intern"],
            start=1,
        )
    ]
    monkeypatch.setattr(
        source, "search_page", lambda query, offset: {"total": 3, "jobPostings": records}
    )
    monkeypatch.setattr(
        source,
        "posting_details",
        lambda path: {
            "startDate": "2026-11-01",
            "jobDescription": future_description
            if path.endswith("R2")
            else "Excel SQL reporting. Develop skills for future opportunities.",
        },
    )
    monkeypatch.setattr(search, "enabled_sources", lambda: [source])
    result = search.run_search(session)
    assert result.new_jobs == result.jobs_found == 1
    assert session.scalar(select(Job)).title == "BI Analyst Intern"
    observations = session.scalars(select(SearchObservation)).all()
    assert sum(row.outcome == "excluded_talent_pool" for row in observations) == 2


def test_confirmed_pool_retires_legacy_import_and_real_vacancy_can_reopen_it(session, monkeypatch):
    future = JobPosting(
        source="ABB Careers",
        source_job_id="R1",
        title="Finance Intern Talent Pool",
        company="ABB",
        location="Krakow",
        url="https://example.com/job/finance-R1",
        description="This role is part of our talent pipeline. Apply for future opportunities.",
    )
    legacy, _ = search.import_posting(session, future)
    identifier = legacy.id
    legacy.notes = "Existing note"
    legacy.application = Application(status="Saved", notes="Existing application")
    other, _ = search.import_posting(
        session,
        JobPosting(
            source="Other Careers",
            source_job_id="R1",
            title="BI Intern",
            company="Other",
            location="Krakow",
            url="https://other.example/job/R1",
            description="Excel SQL.",
        ),
    )
    session.commit()
    future.raw_payload["talent_pool"] = True
    future.url = "https://example.com/job/future-finance-R1"
    postings = [future]

    class Provider:
        name = "ABB Careers"
        scan_complete = True

        def search_jobs(self, criteria):
            return postings

    monkeypatch.setattr(search, "enabled_sources", lambda: [Provider()])
    monkeypatch.setattr(search, "refresh_posting_dates", lambda *args: None)
    run = search.run_search(session)
    assert not session.get(Job, identifier).is_active
    assert other.is_active
    observation = session.scalar(
        select(SearchObservation).where(SearchObservation.search_run_id == run.id)
    )
    assert observation.outcome == "excluded_talent_pool" and observation.job_id == identifier
    postings[0] = JobPosting(
        source="ABB Careers",
        source_job_id="R1",
        title="Finance Intern",
        company="ABB",
        location="Krakow",
        url="https://example.com/job/finance-R1",
        description="Excel SQL.",
    )
    search.run_search(session)
    restored = session.get(Job, identifier)
    assert restored.is_active and restored.description == "Excel SQL."
    assert restored.title == "Finance Intern"
    assert restored.original_url == "https://example.com/job/finance-R1"
    assert restored.notes == "Existing note"
    assert restored.application.notes == "Existing application"


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


@pytest.mark.parametrize("url", ["https://example.com/job/1", "javascript:invalid"])
def test_talent_pools_are_audited_but_not_imported(session, monkeypatch, url) -> None:  # noqa: ANN001
    class Provider:
        name = "Cisco Careers"
        scan_complete = True

        def search_jobs(self, criteria):  # noqa: ANN001, ANN202
            return [
                JobPosting(
                    source=self.name,
                    source_job_id="1",
                    title="Finance Intern",
                    company="Cisco",
                    location="Krakow",
                    url=url,
                    raw_payload={"talent_pool": True},
                )
            ]

    monkeypatch.setattr(search, "enabled_sources", lambda: [Provider()])
    result = search.run_search(session)
    assert result.jobs_found == result.new_jobs == 0
    observation = session.scalar(select(SearchObservation))
    assert observation.outcome == "excluded_talent_pool" and observation.job_id is None


def test_incomplete_scan_is_visible_in_provider_history(session, monkeypatch) -> None:  # noqa: ANN001
    class Provider:
        name = "Incomplete"
        scan_complete = False
        last_search_note = "Employer pagination incomplete"

        def search_jobs(self, criteria):  # noqa: ANN001, ANN202
            return []

    monkeypatch.setattr(search, "enabled_sources", lambda: [Provider()])
    search.run_search(session)
    provider = session.scalar(select(ProviderRun))
    assert provider.status == "partial" and "incomplete" in provider.error_message


def test_workday_cooldown_is_visible_and_skips_requests_to_the_same_host(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = "cooldown.wd5.myworkdayjobs.com"
    source = WorkdaySource(WorkdayConfig("Limited Careers", "Example", host, "tenant", "Careers"))
    other = WorkdaySource(
        WorkdayConfig("Other Careers", "Other", "other.example", "tenant", "Careers")
    )
    calls = []

    def post(url: str, **kwargs) -> httpx.Response:  # noqa: ANN003
        calls.append(url)
        request = httpx.Request("POST", url)
        if url == source.api_url:
            return httpx.Response(429, headers={"Retry-After": "120"}, request=request)
        return httpx.Response(200, json={"total": 0, "jobPostings": []}, request=request)

    def unexpected_get(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Publication lookup must respect the existing Workday cooldown")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", unexpected_get)
    monkeypatch.setattr(search, "enabled_sources", lambda: [source, other])
    start = datetime.now(UTC)
    run = search.run_search(session)
    statuses = {provider.provider: provider.status for provider in run.provider_runs}
    assert statuses == {"Limited Careers": "rate_limited", "Other Careers": "success"}
    assert run.errors == 1 and run.status == "completed_with_errors"
    health = BotController(42, validate_config({})).health(session)["text"]
    assert "Limited Careers · rate_limited · Workday HTTP 429" in health
    with pytest.raises(WorkdayRateLimited) as skipped:
        source.search_page("Krakow", 0)
    assert start + timedelta(minutes=2) <= skipped.value.retry_at
    assert skipped.value.retry_at <= datetime.now(UTC) + timedelta(minutes=2)
    result, _ = lookup_publication_date(f"https://{host}/en-US/Careers/job/Krakow/Test", "Test")
    assert result.value is None
    assert sorted(calls) == sorted([source.api_url, other.api_url])
