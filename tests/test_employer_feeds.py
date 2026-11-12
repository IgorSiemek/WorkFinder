from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.scrapers.base import SearchCriteria
from app.scrapers.career_pages import CareerPageConfig, CareerPageSource
from app.scrapers.employer_feeds import AliorSource, HitachiSource, PhenomSource
from app.scrapers.publication_dates import PublicationDate
from app.scrapers.workday import WorkdayConfig, WorkdaySource


def public_client(monkeypatch, handler):  # noqa: ANN001, ANN201
    factory = httpx.Client
    monkeypatch.setattr(
        "app.scrapers.employer_feeds.httpx.Client",
        lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
    )


def phenom_record(job_id: str, location: str = "Krakow, Poland", **kwargs) -> dict:  # noqa: ANN003
    return {
        "jobId": job_id,
        "title": "Finance Intern",
        "location": location,
        "postedDate": "2026-09-17T10:00:00.000+0000",
        **kwargs,
    }


def test_phenom_reads_later_pages_and_checks_real_location(monkeypatch) -> None:  # noqa: ANN001
    calls = []

    def handler(request):  # noqa: ANN001, ANN202
        calls.append(request)
        records = [phenom_record("1", "Warsaw"), phenom_record("2", RemoteType="Remote")]
        if int(request.url.params["from"]):
            records = [phenom_record("3", RemoteType="Hybrid")]
        data = {"eagerLoadRefineSearch": {"status": 200, "totalHits": 3, "data": {"jobs": records}}}
        return httpx.Response(200, text="<script>phApp.ddo = " + json.dumps(data) + ";</script>")

    public_client(monkeypatch, handler)
    source = PhenomSource(CareerPageConfig("PMI", "PMI", "https://example.com/en/search-results"))
    source.terms = ("intern",)
    jobs = source.search_jobs(SearchCriteria())
    assert [j.source_job_id for j in jobs] == ["3"]
    assert jobs[0].date_posted == date(2026, 9, 17)
    assert jobs[0].remote_type == "Hybrid"
    assert jobs[0].url == "https://example.com/en/job/3/Finance-Intern"
    assert [r.url.params["from"] for r in calls] == ["0", "2"]
    assert all("location" not in r.url.params for r in calls)
    assert source.scan_complete and not source.can_confirm_absence


def test_phenom_repeated_page_does_not_report_complete(monkeypatch) -> None:  # noqa: ANN001
    data = {
        "eagerLoadRefineSearch": {
            "status": 200,
            "totalHits": 20,
            "data": {"jobs": [phenom_record("1")]},
        }
    }
    public_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            text="phApp.ddo = " + json.dumps(data),
        ),
    )
    source = PhenomSource(CareerPageConfig("PMI", "PMI", "https://example.com/search-results"))
    source.terms = ("intern",)
    assert len(source.search_jobs(SearchCriteria())) == 1
    assert not source.scan_complete and not source.can_confirm_absence


def test_phenom_marks_future_talent_pool() -> None:
    source = PhenomSource(CareerPageConfig("Cisco", "Cisco", "https://example.com/search-results"))
    posting = source.posting(
        phenom_record(
            "1",
            descriptionTeaser="This exact role may not be open today.",
        )
    )
    assert posting.raw_payload["talent_pool"]


def test_phenom_does_not_confuse_career_benefits_with_talent_pool() -> None:
    source = PhenomSource(CareerPageConfig("PMI", "PMI", "https://example.com/search-results"))
    posting = source.posting(
        phenom_record(
            "1",
            descriptionTeaser="Working Pattern: Hybrid. Develop skills for future opportunities.",
        )
    )
    assert not posting.raw_payload["talent_pool"]
    assert posting.remote_type == "Hybrid"


def test_missing_public_data_is_error_instead_of_empty_success(monkeypatch) -> None:  # noqa: ANN001
    public_client(monkeypatch, lambda request: httpx.Response(200, text="Careers homepage"))
    source = PhenomSource(CareerPageConfig("Cisco", "Cisco", "https://example.com/search-results"))
    with pytest.raises(ValueError, match="expected public job data"):
        source.search_jobs(SearchCriteria())


def test_hitachi_scans_city_pages_without_restrictive_experience_filter(monkeypatch) -> None:  # noqa: ANN001
    calls = []

    def handler(request):  # noqa: ANN001, ANN202
        calls.append(request)
        second = "/page/2" in request.url.path
        record = {
            "requisitionID": "R2" if second else "R1",
            "title": "Internship - Accounts Payable" if second else "IT Engineer",
            "originalURL": "internship-accounts-payable/job/R2",
            "locations": [{"locationText": "Krakow, Poland"}],
            "customFields": [
                {"cfKey": "unused"},
                {"cfKey": "cf_experience", "value": "Entry Level"},
            ],
        }
        data = {"jobSearch": {"jobs": [record], "totalJob": 2}}
        return httpx.Response(200, text="window.__PRELOAD_STATE__ = " + json.dumps(data))

    public_client(monkeypatch, handler)
    monkeypatch.setattr(
        "app.scrapers.employer_feeds.lookup_publication_date",
        lambda *args: (PublicationDate(date(2026, 9, 17)), {}),
    )
    source = HitachiSource(CareerPageConfig("Hitachi", "Hitachi", "https://example.com/jobs"))
    jobs = source.search_jobs(SearchCriteria())
    assert [j.source_job_id for j in jobs] == ["R2"]
    assert jobs[0].date_posted == date(2026, 9, 17)
    assert len(calls) == 2 and calls[1].url.params["filter[city]"] == "Kraków"
    assert all("filter[cf_experience]" not in r.url.params for r in calls)
    assert source.can_confirm_absence


def test_alior_reads_later_pages_and_dates_from_same_row(monkeypatch) -> None:  # noqa: ANN001
    def handler(request):  # noqa: ANN001, ANN202
        second = request.url.params["page"] == "2"
        html = f"""<table><tr><td><a href="/oferta/{2 if second else 1}.html">Stażysta</a></td>
        <td class="job-location">{"Kraków" if second else "Warszawa"}</td>
        <td class="job-date">17.09.2026</td></tr></table><a href="?page=2">2</a>"""
        return httpx.Response(200, text=html)

    public_client(monkeypatch, handler)
    source = AliorSource(CareerPageConfig("Alior", "Alior", "https://example.com/jobs"))
    jobs = source.search_jobs(SearchCriteria())
    assert len(jobs) == 1 and jobs[0].source_job_id == "2.html"
    assert jobs[0].date_posted == date(2026, 9, 17)
    assert source.can_confirm_absence


def test_generic_monitor_requires_scoped_location_even_with_location_parameter(monkeypatch) -> None:  # noqa: ANN001
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.com/jobs"),
        text="""
    <h1>Kraków careers</h1><section>Warszawa <a href="/job/1">Finance Intern</a></section>
    <a href="/job/2">Finance Intern</a>""",
    )
    monkeypatch.setattr("app.scrapers.career_pages.httpx.get", lambda *args, **kwargs: response)
    source = CareerPageSource(CareerPageConfig("Test", "Test", "https://example.com/jobs", True))
    assert source.search_jobs(SearchCriteria()) == []


def test_generic_monitor_preserves_location_after_nested_lists(monkeypatch) -> None:  # noqa: ANN001
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.com/jobs"),
        text="""
    <article><a href="/job/1">Software Intern</a><ul><li>ID: 1</li><li>Krakow</li></ul></article>
    <article><a href="/job/2">Finance Intern</a><ul><li>ID: 2</li><li>Warsaw</li></ul></article>""",
    )
    monkeypatch.setattr("app.scrapers.career_pages.httpx.get", lambda *args, **kwargs: response)
    source = CareerPageSource(CareerPageConfig("Test", "Test", "https://example.com/jobs"))
    assert [j.title for j in source.search_jobs(SearchCriteria())] == ["Software Intern"]


def test_workday_checks_additional_locations_and_real_description(monkeypatch) -> None:  # noqa: ANN001
    request = httpx.Request("POST", "https://example.com/jobs")
    monkeypatch.setattr(
        "app.scrapers.workday.httpx.post",
        lambda *args, **kwargs: httpx.Response(
            200,
            request=request,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Finance Intern",
                        "locationsText": "2 Locations",
                        "externalPath": "/job/Warsaw/Finance-Intern_R1",
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(
        "app.scrapers.workday.httpx.get",
        lambda *args, **kwargs: httpx.Response(
            200,
            request=request,
            json={
                "jobPostingInfo": {
                    "location": "Warsaw",
                    "additionalLocations": ["Krakow, Poland"],
                    "jobDescription": "<p>Support financial reporting.</p>",
                    "remoteType": "Hybrid",
                }
            },
        ),
    )
    source = WorkdaySource(WorkdayConfig("Test", "Test", "example.com", "test", "Careers"))
    jobs = source.search_jobs(SearchCriteria())
    assert len(jobs) == 1
    assert "Krakow" in jobs[0].location and jobs[0].remote_type == "Hybrid"
    assert jobs[0].description == "Support financial reporting."
