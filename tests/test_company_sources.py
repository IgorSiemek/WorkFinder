from __future__ import annotations

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
            "total": 2,
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
            ],
        }
    )
    monkeypatch.setattr("app.scrapers.workday.httpx.post", lambda *args, **kwargs: response)
    source = WorkdaySource(WorkdayConfig("BBH Careers", "BBH", "example.com", "bbh", "BBH"))
    jobs = source.search_jobs(SearchCriteria(keywords=["finance"]))
    assert len(jobs) == 1
    assert jobs[0].company == "BBH"
    assert jobs[0].source_job_id == "JR1"


def test_career_page_requires_relevant_title_and_krakow_context(monkeypatch) -> None:  # noqa: ANN001
    html = """
    <section>Kraków, Poland <a href="/job/123">Junior Finance Analyst</a></section>
    <section>Warsaw, Poland <a href="/job/456">Finance Analyst Intern</a></section>
    <section>Kraków, Poland <a href="/about">About our analyst teams</a></section>
    """
    response = FakeResponse(text=html)
    monkeypatch.setattr("app.scrapers.career_pages.httpx.get", lambda *args, **kwargs: response)
    source = CareerPageSource(
        CareerPageConfig("Example Careers", "Example", "https://example.com/jobs")
    )
    jobs = source.search_jobs(SearchCriteria(keywords=["finance"]))
    assert [job.title for job in jobs] == ["Junior Finance Analyst"]


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
    }
    companies = {
        source.config.company
        for source in enabled_sources()
        if isinstance(source, (CareerPageSource, WorkdaySource))
    }
    assert expected <= companies
