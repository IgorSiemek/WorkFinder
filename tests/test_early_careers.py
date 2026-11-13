import json
from datetime import date

import httpx
import pytest

from app.scrapers.base import SearchCriteria
from app.scrapers.career_pages import CareerPageConfig
from app.scrapers.early_careers import LundbeckSource, StoneXSource, TEConnectivitySource


def mock_client(monkeypatch, handler):
    factory = httpx.Client
    monkeypatch.setattr(
        "app.scrapers.early_careers.public_client",
        lambda: factory(transport=httpx.MockTransport(handler)),
    )


def sap_description():
    return """<meta itemprop="datePosted" content="Thu Nov 05 00:00:00 UTC 2026">
    <span itemprop="description">Introduction <span>to finance</span></span>
    <span itemprop="description">Excel, SAP and reporting. Hybrid.</span>
    <footer>Cookie consent</footer>"""


def lundbeck_row(identifier, city="Krakow", title="Finance Intern"):
    return f"""<tr><td><a href="/job/Finance-Intern/{identifier}/">{title}</a></td>
    <td class="colLocation hidden-phone">{city}</td></tr>"""


def test_lundbeck_reads_later_pages_and_full_description_without_cookie_text(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/search/":
            rows = (
                lundbeck_row("1", "Warsaw") + lundbeck_row("3", title="Senior Analyst")
                if request.url.params["startrow"] == "0"
                else lundbeck_row("2")
            )
            return httpx.Response(200, text="Results 1 - 2 of 3<table>" + rows + "</table>")
        return httpx.Response(200, text=sap_description())

    mock_client(monkeypatch, handler)
    source = LundbeckSource(
        CareerPageConfig("Lundbeck", "Lundbeck", "https://jobs.lundbeck/search/")
    )
    jobs = source.search_jobs(SearchCriteria(keywords=["SAP"]))
    assert [job.source_job_id for job in jobs] == ["2"]
    assert jobs[0].description == "Introduction to finance Excel, SAP and reporting. Hybrid."
    assert jobs[0].date_posted == date(2026, 11, 5)
    assert jobs[0].remote_type == "Hybrid"
    assert [r.url.params["startrow"] for r in calls if r.url.path == "/search/"] == ["0", "2"]
    assert source.scan_complete and source.can_confirm_absence


@pytest.mark.parametrize("valid_empty", [True, False])
def test_lundbeck_requires_verifiable_empty_result_set(monkeypatch, valid_empty):
    body = "There are currently no open positions matching" if valid_empty else "Careers homepage"
    mock_client(monkeypatch, lambda request: httpx.Response(200, text=body))
    source = LundbeckSource(
        CareerPageConfig("Lundbeck", "Lundbeck", "https://jobs.lundbeck/search/")
    )
    if valid_empty:
        assert source.search_jobs(SearchCriteria()) == []
        assert source.scan_complete
    else:
        with pytest.raises(ValueError, match="verifiable result count"):
            source.search_jobs(SearchCriteria())
        assert not source.can_confirm_absence


def te_record(identifier, city="Krakow", title="Finance Intern"):
    return {
        "response": {
            "id": identifier,
            "jobLocationShort": [city + "<br>"],
            "unifiedStandardTitle": title,
            "unifiedUrlTitle": "Finance-Intern",
            "unifiedStandardStart": "11/5/26",
        }
    }


@pytest.mark.parametrize("incomplete", [False, True])
def test_te_paginates_deduplicates_and_cannot_expire_jobs_after_incomplete_scan(
    monkeypatch, incomplete
):
    pages = []
    details = []

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            page = payload["pageNumber"]
            pages.append(page)
            assert payload["location"] == "Krakow"
            assert request.headers["X-CSRF-Token"] == "test-token"
            records = [te_record("1", title="Accountant"), te_record("2", "Warsaw")]
            if page == 1:
                records = [te_record("1", title="Accountant"), te_record("3")]
            if page >= 2:
                return httpx.Response(200, json={"totalJobs": 4})
            return httpx.Response(
                200, json={"totalJobs": 4 if incomplete else 3, "jobSearchResult": records}
            )
        if "/job/" in request.url.path:
            details.append(request.url.path)
            return httpx.Response(200, text=sap_description())
        return httpx.Response(200, text='"X-CSRF-Token": "test-token"')

    mock_client(monkeypatch, handler)
    source = TEConnectivitySource(
        CareerPageConfig("TE", "TE Connectivity", "https://example.com/search/")
    )
    jobs = source.search_jobs(SearchCriteria())
    assert [job.source_job_id for job in jobs] == ["3"]
    assert len(details) == 1
    assert jobs[0].date_posted == date(2026, 11, 5)
    assert pages == ([0, 1, 2] if incomplete else [0, 1])
    assert source.scan_complete is (not incomplete)
    assert source.can_confirm_absence is (not incomplete)


@pytest.mark.parametrize(
    "payload,valid", [({"totalJobs": 0}, True), ({"totalJobs": 2}, False), ({"jobs": []}, False)]
)
def test_te_empty_response_and_broken_schema(monkeypatch, payload, valid):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    source = TEConnectivitySource(
        CareerPageConfig("TE", "TE Connectivity", "https://example.com/search/")
    )
    if valid:
        assert source.search_jobs(SearchCriteria()) == []
        assert source.scan_complete
    else:
        with pytest.raises(ValueError, match="valid result set"):
            source.search_jobs(SearchCriteria())
        assert not source.can_confirm_absence


def stone_card(identifier, city="Krakow", title="Finance Intern"):
    return f"""<li class="iCIMS_JobCardItem"><div>
    <span>Job Locations</span><span>{city}</span>
    <a href="https://example.com/jobs/{identifier}/finance/job?in_iframe=1">
    <span>Job Posting Title</span><h3>{title}</h3></a></div></li>"""


@pytest.mark.parametrize("missing_city", [False, True])
def test_stonex_scans_later_pages_and_uses_offer_location_not_search_page(
    monkeypatch, missing_city
):
    seen = []

    def handler(request):
        if request.url.path == "/jobs/search":
            page = int(request.url.params["pr"])
            seen.append(page)
            cards = stone_card("1", "Warsaw") if page == 0 else stone_card("2")
            if missing_city:
                cards += stone_card("3", "")
            return httpx.Response(200, text=f"Krakow Page {page + 1} of 2 " + cards)
        return httpx.Response(
            200,
            text='<div class="iCIMS_InfoMsg iCIMS_InfoMsg_Job">'
            "Excel and financial reporting.</div>",
        )

    mock_client(monkeypatch, handler)
    source = StoneXSource(CareerPageConfig("StoneX", "StoneX", "https://example.com/jobs/search"))
    jobs = source.search_jobs(SearchCriteria())
    assert [job.source_job_id for job in jobs] == ["2"]
    assert jobs[0].description == "Excel and financial reporting."
    assert jobs[0].url == "https://example.com/jobs/2/finance/job"
    assert seen == [0, 1]
    assert source.scan_complete is (not missing_city)
    assert source.can_confirm_absence is (not missing_city)


def test_stonex_repeated_page_is_partial(monkeypatch):
    mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="Page 1 of 2 " + stone_card("1", title="Accountant")
        ),
    )
    source = StoneXSource(CareerPageConfig("StoneX", "StoneX", "https://example.com/jobs/search"))
    assert source.search_jobs(SearchCriteria()) == []
    assert not source.scan_complete and not source.can_confirm_absence


def test_lundbeck_missing_city_cannot_confirm_absence(monkeypatch):
    mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="Results 1 - 1 of 1" + lundbeck_row("1", "Poland")
        ),
    )
    source = LundbeckSource(
        CareerPageConfig("Lundbeck", "Lundbeck", "https://jobs.lundbeck/search/")
    )
    assert source.search_jobs(SearchCriteria()) == []
    assert not source.scan_complete and not source.can_confirm_absence


def test_lundbeck_repeated_page_is_partial(monkeypatch):
    mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="Results 1 - 1 of 2" + lundbeck_row("1", title="Accountant")
        ),
    )
    source = LundbeckSource(
        CareerPageConfig("Lundbeck", "Lundbeck", "https://jobs.lundbeck/search/")
    )
    assert source.search_jobs(SearchCriteria()) == []
    assert not source.scan_complete and not source.can_confirm_absence
