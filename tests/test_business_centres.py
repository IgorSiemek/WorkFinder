from datetime import date

import httpx
import pytest

from app.scrapers.base import SearchCriteria
from app.scrapers.business_centres import EuroclearSource, HaviSource, PepsiCoSource
from app.scrapers.career_pages import CareerPageConfig
from app.scrapers.common import relevant_to_krakow
from app.scrapers.workday import WorkdayConfig, WorkdaySource


def public_client(monkeypatch, handler):
    factory = httpx.Client
    monkeypatch.setattr(
        "app.scrapers.business_centres.httpx.Client",
        lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_havi_reads_later_pages_and_preserves_description_and_publication_date(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if "/job/" in request.url.path:
            return httpx.Response(
                200,
                text="""<span itemprop="description">
                Finance <span>Excel <span>and SAP</span></span> reporting.
                </span><footer>Cookie policy</footer>""",
            )
        offset = int(request.url.params["startrow"])
        if not offset:
            rows = """<tr><td><a href="/job/1/">Finance Analyst</a></td>
                <td class="colLocation">Krakow</td></tr>
                <tr><td><a href="/job/2/">Finance Intern</a></td>
                <td class="colLocation">Warsaw</td></tr>"""
        else:
            rows = """<tr><td><a href="/job/3/">Finance Intern</a></td>
                <td class="colLocation hidden-phone">Krakow, PL</td>
                <td class="colDate">05 Nov 2026</td></tr>"""
        return httpx.Response(200, text="Results 1 – 2 of 3<table>" + rows + "</table>")

    public_client(monkeypatch, handler)
    source = HaviSource(CareerPageConfig("HAVI", "HAVI", "https://example.com/search/"))
    jobs = source.search_jobs(SearchCriteria())
    assert [job.source_job_id for job in jobs] == ["3"]
    assert jobs[0].description == "Finance Excel and SAP reporting."
    assert jobs[0].date_posted == date(2026, 11, 5)
    assert source.scan_complete and source.can_confirm_absence
    assert [r.url.params["startrow"] for r in calls if "/job/" not in r.url.path] == ["0", "2"]


def test_havi_repeated_result_page_is_partial(monkeypatch):
    html = """Results 1 – 1 of 2<table><tr><td><a href="/job/1/">Engineer</a></td>
        <td class="colLocation">Krakow</td></tr></table>"""
    public_client(monkeypatch, lambda request: httpx.Response(200, text=html))
    source = HaviSource(CareerPageConfig("HAVI", "HAVI", "https://example.com/search/"))
    assert source.search_jobs(SearchCriteria()) == []
    assert not source.scan_complete and not source.can_confirm_absence


def test_havi_missing_city_cannot_expire_previous_offers(monkeypatch):
    html = 'Results 1 – 1 of 1<table><tr><td><a href="/job/1/">Finance Intern</a></td></tr></table>'
    public_client(monkeypatch, lambda request: httpx.Response(200, text=html))
    source = HaviSource(CareerPageConfig("HAVI", "HAVI", "https://example.com/search/"))
    assert source.search_jobs(SearchCriteria()) == []
    assert not source.scan_complete and not source.can_confirm_absence
    assert "city metadata" in source.last_search_note


@pytest.mark.parametrize("valid_empty", [True, False])
def test_havi_distinguishes_empty_search_from_changed_markup(monkeypatch, valid_empty):
    text = "There are currently no open positions matching" if valid_empty else "Careers homepage"
    public_client(monkeypatch, lambda request: httpx.Response(200, text=text))
    source = HaviSource(CareerPageConfig("HAVI", "HAVI", "https://example.com/search/"))
    if valid_empty:
        assert source.search_jobs(SearchCriteria()) == []
        assert source.scan_complete
    else:
        with pytest.raises(ValueError, match="result count"):
            source.search_jobs(SearchCriteria())


def pepsi_record(identifier, city="Krakow", **values):
    return {
        "data": {
            "slug": identifier,
            "req_id": identifier,
            "title": "Finance Intern",
            "full_location": city,
            "description": "Excel, financial reporting. Hybrid work.",
            "posted_date": "2026-11-05T21:07:00+0000",
            **values,
        }
    }


def test_pepsico_paginates_and_checks_each_roles_city_and_remote_type(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.params["page"] == "1":
            rows = [
                pepsi_record(
                    "1",
                    "Warsaw",
                    meta_data={
                        "googlejobs": {
                            "derivedInfo": {
                                "locations": [{"postalAddress": {"locality": "Krakow"}}],
                            }
                        }
                    },
                ),
                pepsi_record("2", remote=True),
            ]
        else:
            rows = [pepsi_record("3"), pepsi_record("4", title="Senior Finance Analyst")]
        return httpx.Response(200, json={"totalCount": 4, "jobs": rows})

    public_client(monkeypatch, handler)
    source = PepsiCoSource(CareerPageConfig("PepsiCo", "PepsiCo", "https://example.com/main/jobs"))
    jobs = source.search_jobs(SearchCriteria())
    assert [job.source_job_id for job in jobs] == ["1", "3"]
    assert jobs[0].remote_type == "Hybrid"
    assert jobs[1].url == "https://example.com/main/jobs/3?lang=en-us"
    assert jobs[1].date_posted == date(2026, 11, 5)
    assert all(request.url.params["lang"] == "en-us" for request in calls)
    assert source.scan_complete and source.can_confirm_absence


def test_pepsico_repeated_page_cannot_confirm_absence(monkeypatch):
    public_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"totalCount": 3, "jobs": [pepsi_record("1")]},
        ),
    )
    source = PepsiCoSource(CareerPageConfig("PepsiCo", "PepsiCo", "https://example.com/main/jobs"))
    assert len(source.search_jobs(SearchCriteria())) == 1
    assert not source.scan_complete and not source.can_confirm_absence


def test_pepsico_country_only_internship_cannot_expire_previous_offers(monkeypatch):
    public_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"totalCount": 1, "jobs": [pepsi_record("1", "Poland")]},
        ),
    )
    source = PepsiCoSource(CareerPageConfig("PepsiCo", "PepsiCo", "https://example.com/main/jobs"))
    assert source.search_jobs(SearchCriteria()) == []
    assert not source.scan_complete and not source.can_confirm_absence
    assert "city metadata" in source.last_search_note


def test_pepsico_missing_result_schema_is_an_error(monkeypatch):
    public_client(monkeypatch, lambda request: httpx.Response(200, json={"jobs": []}))
    source = PepsiCoSource(CareerPageConfig("PepsiCo", "PepsiCo", "https://example.com/main/jobs"))
    with pytest.raises(ValueError, match="valid result set"):
        source.search_jobs(SearchCriteria())


def test_euroclear_country_only_offers_remain_unverified_and_city_metadata_is_used(monkeypatch):
    def handler(request):
        if request.url.path.endswith("recruitingCEJobRequisitions"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "TotalJobsCount": 3,
                            "requisitionList": [
                                {"Id": identifier, "Title": title, "PostedDate": "2026-11-05"}
                                for identifier, title in [
                                    ("1", "Finance Intern"),
                                    ("2", "Data Intern"),
                                    ("3", "Analyst"),
                                ]
                            ],
                        }
                    ]
                },
            )
        identifier = request.url.params["finder"].split("Id=", 1)[1].split(",", 1)[0]
        locations = [{"TownOrCity": "Krakow"}] if identifier == "2" else [{"TownOrCity": None}]
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "Id": identifier,
                        "Title": "Finance Intern",
                        "PrimaryLocation": "Poland",
                        "PrimaryLocationCountry": "PL",
                        "workLocation": locations,
                        "ExternalDescriptionStr": "<p>Financial reporting and Excel.</p>",
                    }
                ]
            },
        )

    public_client(monkeypatch, handler)
    source = EuroclearSource(
        CareerPageConfig(
            "Euroclear",
            "Euroclear",
            "https://example.com/hcmUI/CandidateExperience/en/sites/CX_1003/jobs",
        )
    )
    jobs = source.search_jobs(SearchCriteria())
    assert len(jobs) == 2
    assert jobs[0].location == "Poland"
    assert jobs[0].raw_payload["location_unverified"]
    assert not relevant_to_krakow(jobs[0].location, jobs[0].description, remote=False)
    assert jobs[1].location == "Poland; Krakow"
    assert not jobs[1].raw_payload["location_unverified"]
    assert jobs[1].date_posted == date(2026, 11, 5)
    assert not source.can_confirm_absence and not source.scan_complete
    assert "1 Poland internships" in source.last_search_note


def test_workday_uses_the_employers_city_spelling(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(
            200, request=httpx.Request("POST", url), json={"total": 0, "jobPostings": []}
        )

    monkeypatch.setattr("app.scrapers.workday.httpx.post", post)
    config = WorkdayConfig(
        "Electrolux", "Electrolux", "example.com", "tenant", "site", search_text="Cracow"
    )
    source = WorkdaySource(config)
    assert source.search_jobs(SearchCriteria()) == []
    assert calls[0]["searchText"] == "Cracow"
    assert source.can_confirm_absence
