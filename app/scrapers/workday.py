from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, JobSource, SearchCriteria
from app.scrapers.common import plain_text, relevant_to_krakow


@dataclass(slots=True)
class WorkdayConfig:
    name: str
    company: str
    host: str
    tenant: str
    site: str


class WorkdaySource(JobSource):
    """Read a public Workday careers search endpoint without authentication."""

    def __init__(self, config: WorkdayConfig) -> None:
        self.config = config
        self.name = config.name
        self.description = f"Official {config.company} public Workday careers feed."

    @property
    def api_url(self) -> str:
        return f"https://{self.config.host}/wday/cxs/{self.config.tenant}/{self.config.site}/jobs"

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        seen_paths: set[str] = set()
        keywords = [keyword.casefold() for keyword in criteria.keywords]
        offset = 0
        while offset < 200:
            response = httpx.post(
                self.api_url,
                json={
                    "appliedFacets": {},
                    "limit": 20,
                    "offset": offset,
                    "searchText": "Krakow",
                },
                timeout=settings.request_timeout_seconds,
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            postings = payload.get("jobPostings", [])
            if not postings:
                break
            for item in postings:
                title = plain_text(item.get("title"))
                location = plain_text(item.get("locationsText"))
                text = f"{title} {location}".casefold()
                path = str(item.get("externalPath") or "")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                if keywords and not any(keyword in text for keyword in keywords):
                    continue
                if not relevant_to_krakow(location, "", remote=False):
                    continue
                url = f"https://{self.config.host}/en-US/{self.config.site}{path}"
                jobs.append(
                    JobPosting(
                        source=self.name,
                        source_job_id=path.rsplit("_", 1)[-1],
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=location or "Kraków",
                        remote_type="Hybrid" if "hybrid" in text else "On-site",
                        description=(
                            f"Official {self.config.company} listing. "
                            f"{title}. Location: {location}."
                        ),
                        raw_payload=item,
                    )
                )
            offset += len(postings)
            if offset >= int(payload.get("total", 0)):
                break
        return jobs
