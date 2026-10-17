from __future__ import annotations

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, JobSource, SearchCriteria
from app.scrapers.common import is_internship_role, plain_text, relevant_to_krakow
from app.scrapers.publication_dates import PublicationDate, date_evidence, source_date


class ArbeitnowSource(JobSource):
    name = "Arbeitnow"
    description = "Public Arbeitnow API; only non-remote Kraków listings are accepted."
    api_url = "https://www.arbeitnow.com/api/job-board-api"

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        response = httpx.get(
            self.api_url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        response.raise_for_status()
        jobs: list[JobPosting] = []
        keywords = [keyword.casefold() for keyword in criteria.keywords]
        for item in response.json().get("data", []):
            description = plain_text(item.get("description"))
            title = plain_text(item.get("title"))
            location = plain_text(item.get("location"))
            remote = bool(item.get("remote"))
            employment_type = ", ".join(item.get("job_types") or []) or "Not specified"
            if not is_internship_role(title, employment_type):
                continue
            text = f"{title} {description}".casefold()
            if keywords and not any(keyword in text for keyword in keywords):
                continue
            if not relevant_to_krakow(location, description, remote):
                continue
            posted = source_date(item.get("created_at"))
            evidence = date_evidence(
                PublicationDate(posted, "Job board created_at", str(item.get("created_at")))
                if posted
                else PublicationDate(),
                item["url"],
            )
            jobs.append(
                JobPosting(
                    source=self.name,
                    source_job_id=str(item.get("slug") or item.get("url")),
                    url=item["url"],
                    title=title,
                    company=plain_text(item.get("company_name")) or "Unknown company",
                    location=location,
                    remote_type="On-site",
                    employment_type=employment_type,
                    description=description,
                    date_posted=posted,
                    raw_payload={**item, "_publication_check": evidence},
                )
            )
        return jobs
