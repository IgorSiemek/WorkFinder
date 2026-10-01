from __future__ import annotations

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, JobSource, SearchCriteria
from app.scrapers.common import parse_date, plain_text, relevant_to_krakow


class RemoteOkSource(JobSource):
    name = "Remote OK"
    description = "Public Remote OK feed; includes only roles available from Poland/Europe."
    api_url = "https://remoteok.com/api"

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        response = httpx.get(
            self.api_url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        jobs: list[JobPosting] = []
        keywords = [keyword.casefold() for keyword in criteria.keywords]
        for item in payload[1:]:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            description = plain_text(item.get("description"))
            title = plain_text(item.get("position"))
            location = plain_text(item.get("location")) or "Remote"
            text = f"{title} {description} {' '.join(item.get('tags') or [])}".casefold()
            if keywords and not any(keyword in text for keyword in keywords):
                continue
            if not relevant_to_krakow(location, description, remote=True):
                continue
            source_id = str(item.get("id"))
            jobs.append(
                JobPosting(
                    source=self.name,
                    source_job_id=source_id,
                    url=item.get("url") or f"https://remoteok.com/remote-jobs/{source_id}",
                    title=title,
                    company=plain_text(item.get("company")) or "Unknown company",
                    location="Kraków / Remote",
                    remote_type="Remote",
                    employment_type="Full-time",
                    description=description,
                    date_posted=parse_date(item.get("date") or item.get("epoch")),
                    salary_min=item.get("salary_min"),
                    salary_max=item.get("salary_max"),
                    salary_currency="USD" if item.get("salary_min") else None,
                    raw_payload=item,
                )
            )
        return jobs
