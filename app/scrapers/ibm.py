from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.career_pages import CareerPageSource
from app.scrapers.common import is_internship_role, plain_text, relevant_to_krakow
from app.scrapers.early_careers import public_client
from app.scrapers.employer_feeds import matches


class IBMSource(CareerPageSource):
    """Use the public search index behind IBM's official careers page."""

    endpoint = "https://www-api.ibm.com/search/api/v2"
    location_field = "field_keyword_19"
    workplace_field = "field_keyword_17"
    employment_field = "field_keyword_18"

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        self.last_search_note = "IBM search incomplete"
        seen: set[str] = set()
        results: list[JobPosting] = []
        offset = 0
        krakow_locations = ["Krakow, PL", "Kraków, PL", "Cracow, PL"]
        location_filter = {"terms": {self.location_field: krakow_locations}}
        with public_client() as client:
            for _ in range(10):
                response = client.post(
                    self.endpoint,
                    json={
                        "appId": "careers",
                        "scopes": ["careers2"],
                        "query": {"bool": {"must": [location_filter]}},
                        "_source": [
                            "title",
                            "url",
                            "description",
                            self.location_field,
                            self.workplace_field,
                            self.employment_field,
                        ],
                        "from": offset,
                        "size": 100,
                    },
                )
                response.raise_for_status()
                data = response.json()
                hits = data["hits"]
                for hit in hits["hits"]:
                    item = hit["_source"]
                    url = item["url"]
                    link = urlsplit(url)
                    identifier = parse_qs(link.query).get("jobId", [""])[0]
                    if (
                        link.scheme != "https"
                        or link.netloc != "careers.ibm.com"
                        or link.path != "/careers/JobDetail"
                        or not identifier.isdecimal()
                    ):
                        raise ValueError("Unexpected IBM job URL")
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    title = plain_text(item["title"])
                    city = plain_text(item[self.location_field])
                    employment = plain_text(item.get(self.employment_field))
                    workplace = plain_text(item.get(self.workplace_field))
                    if not is_internship_role(title, employment) or not relevant_to_krakow(
                        city, "", workplace.casefold() == "remote"
                    ):
                        continue
                    job = JobPosting(
                        source=self.name,
                        source_job_id=identifier,
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=city,
                        description=plain_text(item.get("description")),
                        employment_type=employment,
                        remote_type="Hybrid" if workplace.casefold() == "hybrid" else "On-site",
                    )
                    if matches(job, criteria):
                        results.append(job)
                offset += len(hits["hits"])
                if offset == hits["total"]["value"]:
                    self.scan_complete = self.can_confirm_absence = (
                        hits["total"]["relation"] == "eq"
                        and not data.get("timed_out", False)
                        and not data.get("_shards", {}).get("failed")
                        and len(seen) == offset
                    )
                    break
                if not hits["hits"]:
                    break
        if self.scan_complete:
            self.last_search_note = ""
        return results
