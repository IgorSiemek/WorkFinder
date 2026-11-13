from __future__ import annotations

from datetime import date

import httpx

from app.scrapers.base import SearchCriteria
from app.scrapers.career_pages import CareerPageConfig, CareerPageSource
from app.scrapers.registry import enabled_sources
from app.scrapers.workday import WorkdayConfig, WorkdaySource


class FakeResponse:
    def __init__(self, payload=None, text: str = "", url: str = "https://example.com/jobs") -> None:
        self._payload = payload
        self.text = text
        self.url = url

    def raise_for_status(self) -> None:
        pass

    def json(self):  # noqa: ANN201
        return self._payload


def test_workday_source_keeps_only_relevant_krakow_jobs(monkeypatch) -> None:  # noqa: ANN001
    response = FakeResponse(
        {
            "total": 3,
            "jobPostings": [
                {
                    "title": "Finance Analyst Intern",
                    "locationsText": "Krakow, Poland",
                    "externalPath": "/job/Krakow/Finance-Analyst_JR1",
                },
                {
                    "title": "Finance Analyst Intern",
                    "locationsText": "Boston, USA",
                    "externalPath": "/job/Boston/Finance-Analyst_JR2",
                },
                {
                    "title": "Junior Finance Analyst",
                    "locationsText": "Krakow, Poland",
                    "externalPath": "/job/Krakow/Junior-Finance-Analyst_JR3",
                },
            ],
        }
    )
    monkeypatch.setattr("app.scrapers.workday.httpx.post", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        "app.scrapers.workday.httpx.get",
        lambda *args, **kwargs: FakeResponse(
            {"jobPostingInfo": {"startDate": "2026-09-17", "timeType": "Full time"}}
        ),
    )
    source = WorkdaySource(WorkdayConfig("BBH Careers", "BBH", "example.com", "bbh", "BBH"))
    jobs = source.search_jobs(SearchCriteria(keywords=["finance"]))
    assert len(jobs) == 1
    assert jobs[0].company == "BBH"
    assert jobs[0].source_job_id == "JR1"
    assert jobs[0].date_posted == date(2026, 9, 17)


def test_workday_keeps_offer_when_publication_date_lookup_fails(monkeypatch) -> None:  # noqa: ANN001
    response = FakeResponse(
        {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Finance Intern",
                    "locationsText": "Krakow, Poland",
                    "externalPath": "/job/Krakow/Finance-Intern_R1",
                    "postedOn": "Posted 30+ Days Ago",
                }
            ],
        }
    )
    monkeypatch.setattr("app.scrapers.workday.httpx.post", lambda *args, **kwargs: response)

    def unavailable(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise httpx.ReadTimeout("Detail page timed out")

    monkeypatch.setattr("app.scrapers.workday.httpx.get", unavailable)
    source = WorkdaySource(WorkdayConfig("Example", "Example", "example.com", "example", "Careers"))
    jobs = source.search_jobs(SearchCriteria(keywords=["finance"]))
    assert len(jobs) == 1
    assert jobs[0].date_posted is None
    assert source.can_confirm_absence


def test_career_page_requires_relevant_title_and_krakow_context(monkeypatch) -> None:  # noqa: ANN001
    html = """
    <section>Kraków, Poland <a href="/job/123">Finance Trainee</a></section>
    <section>Warsaw, Poland <a href="/job/456">Finance Analyst Intern</a></section>
    <section>Kraków, Poland <a href="/job/789">Junior Finance Analyst</a>
    Join our team and mentor an intern.</section>
    <section>Kraków, Poland <a href="/about">About our analyst teams</a></section>
    """
    response = FakeResponse(text=html)
    monkeypatch.setattr("app.scrapers.career_pages.httpx.get", lambda *args, **kwargs: response)
    source = CareerPageSource(
        CareerPageConfig("Example Careers", "Example", "https://example.com/jobs")
    )
    jobs = source.search_jobs(SearchCriteria(keywords=["finance"]))
    assert [job.title for job in jobs] == ["Finance Trainee"]


def test_all_requested_krakow_companies_have_enabled_sources() -> None:
    expected = {
        "Hitachi",
        "BBH",
        "UBS",
        "HID",
        "HSBC",
        "Aptiv",
        "Alior",
        "Capgemini",
        "Philip Morris International",
        "HEINEKEN Global Shared Services",
        "Cisco",
        "State Street",
        "Motorola Solutions",
        "Rolls-Royce",
    }
    companies = {
        source.config.company
        for source in enabled_sources()
        if isinstance(source, (CareerPageSource, WorkdaySource))
    }
    assert expected <= companies
