import json
from datetime import date

import httpx

from app.scrapers.base import SearchCriteria
from app.scrapers.career_pages import CareerPageConfig
from app.scrapers.structured_careers import SmartRecruitersSource, TauronSource


def test_smartrecruiters_paginates_and_uses_current_public_job_details(monkeypatch):
    requested_offsets = []

    def handler(request):
        if request.url.path.endswith("/postings"):
            offset = int(request.url.params["offset"])
            requested_offsets.append(offset)
            return httpx.Response(
                200,
                json={"totalFound": 2, "content": [{"id": str(offset + 1), "name": "SQL Intern"}]},
            )
        if request.url.path.endswith("/1"):
            return httpx.Response(410)
        return httpx.Response(
            200,
            json={
                "name": "SQL Intern",
                "jobAdId": "stable-ad-2",
                "active": True,
                "visibility": "PUBLIC",
                "location": {"city": "Kraków", "hybrid": True},
                "postingUrl": "https://jobs.smartrecruiters.com/Test/2-sql-intern",
                "releasedDate": "2025-01-16T12:00:00Z",
                "typeOfEmployment": {"label": "Intern"},
                "jobAd": {"sections": {"jobDescription": {"text": "<p>SQL and data quality.</p>"}}},
            },
        )

    monkeypatch.setattr(
        "app.scrapers.structured_careers.public_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    source = SmartRecruitersSource(
        CareerPageConfig("GGS", "GGS", "https://jobs.smartrecruiters.com/Test")
    )
    jobs = source.search_jobs(SearchCriteria(keywords=["data quality"]))
    assert requested_offsets == [0, 1]
    assert [job.source_job_id for job in jobs] == ["stable-ad-2"]
    assert jobs[0].date_posted == date(2025, 1, 16) and jobs[0].remote_type == "Hybrid"
    assert source.scan_complete


def test_tauron_reads_later_pages_and_rejects_other_companies_and_cities(monkeypatch):
    def row(identifier, city, company):
        return (
            f"<tr jobOfferId='{identifier}' offerId='{identifier}' externalJobOfferId='3' "
            "externalJobOfferRegionId='4' comId='5' skkResult='offer'>"
            "<td>Stażysta w Biurze Rozwoju Organizacji</td>"
            f"<td>{city}</td><td>Staż</td><td>Staż/praktyka</td>"
            f"<td>{company}</td><td>27.11.</td></tr>"
        )

    def handler(request):
        if request.url.path.endswith("GetHtml.ashx"):
            page = request.url.params["pn"]
            content = {
                "1": row("1", "Kraków", "TAURON Dystrybucja Pomiary sp. z o.o."),
                "2": row("2", "Opole", "TAURON Dystrybucja S.A.")
                + row("3", "Krak&#243;w", "TAURON Dystrybucja S.A."),
            }[page]
            content += f"<tr tp='2' tr='3' pn='{page}'></tr>"
            return httpx.Response(200, text=f"workfinder({json.dumps({'htm': content})});")
        assert request.url.params["oid"] == "3"
        return httpx.Response(
            200, text='<div id="JobTitle">Staż</div><p>Praca hybrydowa. Analizy.</p>'
        )

    monkeypatch.setattr(
        "app.scrapers.structured_careers.public_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    source = TauronSource(CareerPageConfig("TAURON", "TAURON Dystrybucja", "https://example.com"))
    jobs = source.search_jobs(SearchCriteria(keywords=["analizy"]))
    assert [job.source_job_id for job in jobs] == ["3"]
    assert jobs[0].location == "Kraków" and jobs[0].remote_type == "Hybrid"
    assert jobs[0].date_posted is None and jobs[0].raw_payload["deadline_label"] == "27.11."
    assert source.scan_complete
