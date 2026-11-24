from __future__ import annotations

import httpx

from app.scrapers.base import SearchCriteria
from app.scrapers.career_pages import CareerPageConfig
from app.scrapers.deloitte import DeloitteSource
from app.scrapers.ibm import IBMSource


def test_ibm_uses_index_location_and_keeps_only_local_internships(monkeypatch) -> None:  # noqa: ANN001
    def row(identifier: int, title: str, city: str, employment: str) -> dict:
        return {
            "_source": {
                "title": title,
                "url": f"https://careers.ibm.com/careers/JobDetail?jobId={identifier}",
                "description": "Software engineering and cloud infrastructure.",
                "field_keyword_17": "Hybrid",
                "field_keyword_18": employment,
                "field_keyword_19": city,
            }
        }

    data = {
        "hits": {
            "total": {"value": 4, "relation": "eq"},
            "hits": [
                row(146219, "Software Developer Intern 2027", "Krakow, PL", "Internship"),
                row(1, "Software Developer Intern", "Warsaw, PL", "Internship"),
                row(2, "Senior Software Developer", "Krakow, PL", "Regular"),
                row(3, "Software Intern Talent Pool", "Krakow, PL", "Internship"),
            ],
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "www-api.ibm.com"
        return httpx.Response(200, json=data)

    monkeypatch.setattr(
        "app.scrapers.ibm.public_client",
        lambda: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    source = IBMSource(CareerPageConfig("IBM Careers", "IBM", "https://www.ibm.com/careers/search"))
    jobs = source.search_jobs(SearchCriteria(keywords=["intern"]))
    assert [job.source_job_id for job in jobs] == ["146219", "3"]
    assert jobs[0].remote_type == "Hybrid"
    assert jobs[0].raw_payload["talent_pool"] is False
    assert jobs[1].raw_payload["talent_pool"] is True
    assert source.scan_complete


def test_deloitte_follows_pages_and_keeps_detail_for_krakow(monkeypatch) -> None:  # noqa: ANN001
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/2"):
            return httpx.Response(
                200, text='<article class="icon-description"><p>Excel and processes</p></article>'
            )
        offset = int(request.url.params["jobOffset"])
        if offset == 0:
            title, city = "Finance Intern", "Warsaw - Poland"
        else:
            assert offset == 1
            title, city = "Business Analyst Intern", "Krakow, Warsaw - Poland"
        html = f"""
            <div>Displaying {offset + 1}-{offset + 1} of 2</div>
            <article class="article article--result">
                <a href="/en_US/careers/JobDetail/{offset + 1}">{title}</a>
                <div class="article__header__text__subtitle">
                    <span>{city}</span><span>Student</span><span>Hybrid</span>
                </div>
            </article>
        """
        return httpx.Response(200, text=html)

    monkeypatch.setattr(
        "app.scrapers.deloitte.public_client",
        lambda: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    source = DeloitteSource(
        CareerPageConfig(
            "Deloitte Careers", "Deloitte", "https://apply.deloittece.com/en_US/careers/SearchJobs/"
        )
    )
    jobs = source.search_jobs(SearchCriteria(keywords=["intern"]))
    assert [job.source_job_id for job in jobs] == ["2"]
    assert jobs[0].description == "Excel and processes"
    assert jobs[0].remote_type == "Hybrid"
    assert source.scan_complete
