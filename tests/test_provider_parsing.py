from __future__ import annotations

from app.scrapers.arbeitnow import ArbeitnowSource
from app.scrapers.base import SearchCriteria
from app.scrapers.common import is_senior_role, relevant_to_krakow


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "data": [
                {
                    "slug": "one",
                    "url": "https://example.com/one",
                    "title": "Finance Analyst Intern",
                    "company_name": "Acme",
                    "location": "Kraków",
                    "remote": False,
                    "description": "<p>Excel and financial analysis</p>",
                    "created_at": "2026-09-30T08:00:00Z",
                },
                {
                    "slug": "remote-linguist",
                    "url": "https://www.arbeitnow.ch/jobs/companies/lilt-production/"
                    "linguist-mongolian-ui-technical-marketing-remote-165068",
                    "title": "Finance Linguist - Remote",
                    "company_name": "LILT",
                    "location": "Remote",
                    "remote": True,
                    "description": "Financial marketing work from anywhere in Europe",
                    "created_at": "2026-09-30T08:00:00Z",
                },
                {
                    "slug": "two",
                    "url": "https://example.com/two",
                    "title": "Finance Analyst Intern",
                    "company_name": "Acme",
                    "location": "Berlin",
                    "remote": False,
                    "description": "Excel and financial analysis",
                    "created_at": "2026-09-30T08:00:00Z",
                },
                {
                    "slug": "regular-job",
                    "url": "https://example.com/regular-job",
                    "title": "Junior Finance Analyst",
                    "company_name": "Acme",
                    "location": "Kraków",
                    "remote": False,
                    "description": "Finance reporting. Previous internship experience welcome.",
                },
            ]
        }


def test_provider_filters_out_non_krakow_jobs(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("app.scrapers.arbeitnow.httpx.get", lambda *args, **kwargs: FakeResponse())
    jobs = ArbeitnowSource().search_jobs(SearchCriteria(keywords=["finance"]))
    assert len(jobs) == 1
    assert jobs[0].location == "Kraków"


def test_provider_uses_structured_internship_type(monkeypatch) -> None:  # noqa: ANN001
    class TypedResponse(FakeResponse):
        def json(self) -> dict:
            return {
                "data": [
                    {
                        "slug": "structured-internship",
                        "url": "https://example.com/typed",
                        "title": "Financial Analyst",
                        "location": "Kraków",
                        "job_types": ["Internship", "Part-time"],
                    }
                ]
            }

    monkeypatch.setattr("app.scrapers.arbeitnow.httpx.get", lambda *args, **kwargs: TypedResponse())
    jobs = ArbeitnowSource().search_jobs(SearchCriteria(keywords=["financial"]))
    assert len(jobs) == 1
    assert jobs[0].employment_type == "Internship, Part-time"


def test_only_krakow_location_spellings_are_accepted() -> None:
    assert relevant_to_krakow("Kraków, Poland", "", remote=False)
    assert relevant_to_krakow("Krakow", "", remote=False)
    assert relevant_to_krakow("Cracow", "", remote=False)
    assert not relevant_to_krakow("Remote", "Work from anywhere in Europe", remote=True)
    assert not relevant_to_krakow("Berlin", "The company has an office in Krakow", remote=False)
    assert not relevant_to_krakow("Krakow", "", remote=True)


def test_senior_titles_are_excluded() -> None:
    blocked = [
        "Senior Financial Accounting & Tax Specialist",
        "Lead Data Analyst",
        "Solutions Architect",
        "Finance Manager",
        "Starszy Analityk Finansowy",
    ]
    assert all(is_senior_role(title) for title in blocked)
    assert not is_senior_role("Junior Finance Analyst")
    assert not is_senior_role("Accounting Intern")
