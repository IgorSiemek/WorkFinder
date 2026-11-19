from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from app.scrapers.base import SearchCriteria
from app.scrapers.workday import WorkdayConfig, WorkdaySource
from app.scrapers.workday_http import WorkdayHttp
from app.scrapers.workday_limits import WorkdayRateLimited


def test_challenged_scan_uses_one_browser_for_pages_and_details(monkeypatch):
    source = WorkdaySource(
        WorkdayConfig("Browser Careers", "Example", "browser.myworkdayjobs.com", "tenant", "Jobs")
    )
    direct_calls, browser_calls, closed = [], [], []

    def direct(url, **kwargs):
        direct_calls.append(url)
        return httpx.Response(
            429, headers={"cf-mitigated": "challenge"}, request=httpx.Request("POST", url)
        )

    class Browser:
        def close(self):
            closed.append(True)

    def start(client, url):
        client.browser = Browser()
        client.page = object()

    def request(client, method, url, options):
        browser_calls.append((method, url, options))
        if method == "GET":
            data = {"jobPostingInfo": {"startDate": "2026-11-18"}}
        else:
            offset = options["json"]["offset"]
            data = {
                "total": 2,
                "jobPostings": [
                    {
                        "title": "Finance Intern",
                        "locationsText": "Krakow, Poland",
                        "externalPath": f"/job/Krakow/Intern_R{offset}",
                    }
                ],
            }
        return httpx.Response(200, json=data, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "post", direct)
    monkeypatch.setattr(WorkdayHttp, "start_browser", start)
    monkeypatch.setattr(WorkdayHttp, "browser_request", request)
    jobs = source.search_jobs(SearchCriteria(keywords=["finance"]))
    assert [job.source_job_id for job in jobs] == ["R0", "R1"]
    assert source.scan_complete and source.can_confirm_absence
    assert direct_calls == [source.api_url]
    assert [method for method, _, _ in browser_calls] == ["POST", "GET", "POST", "GET"]
    assert closed == [True] and source.http is None


def test_actual_rate_limit_does_not_start_a_browser(monkeypatch):
    url = "https://limited.myworkdayjobs.com/wday/cxs/tenant/Jobs/jobs"
    retry_at = datetime(2030, 1, 1, tzinfo=UTC)

    def post(url, **kwargs):
        return httpx.Response(
            429,
            headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
            request=httpx.Request("POST", url),
        )

    def unexpected_browser(*args):
        pytest.fail("An ordinary rate limit must be respected")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(WorkdayHttp, "start_browser", unexpected_browser)
    with WorkdayHttp() as client, pytest.raises(WorkdayRateLimited) as error:
        client.request("POST", url)
    assert error.value.retry_at == retry_at


def test_browser_failure_releases_resources_and_preserves_rate_limit(monkeypatch):
    url = "https://still-blocked.myworkdayjobs.com/wday/cxs/tenant/Jobs/jobs"

    def challenge(url, **kwargs):
        return httpx.Response(
            429, headers={"cf-mitigated": "challenge"}, request=httpx.Request("POST", url)
        )

    def start(client, url):
        client.page = object()

    def blocked(client, method, url, options):
        return challenge(url)

    monkeypatch.setattr(httpx, "post", challenge)
    monkeypatch.setattr(WorkdayHttp, "start_browser", start)
    monkeypatch.setattr(WorkdayHttp, "browser_request", blocked)
    with WorkdayHttp() as client, pytest.raises(WorkdayRateLimited):
        client.request("POST", url)
    assert not client.has_slot
    with WorkdayHttp() as next_client, pytest.raises(WorkdayRateLimited):
        next_client.request("POST", url)
