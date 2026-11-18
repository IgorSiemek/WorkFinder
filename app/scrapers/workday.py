from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, JobSource, SearchCriteria
from app.scrapers.common import is_internship_role, plain_text, relevant_to_krakow
from app.scrapers.publication_dates import PublicationDate, date_evidence, source_date
from app.scrapers.workday_limits import WorkdayRateLimited, workday_access

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkdayConfig:
    name: str
    company: str
    host: str
    tenant: str
    site: str
    search_text: str = "Krakow"


class WorkdaySource(JobSource):
    """Read a public Workday careers search endpoint without authentication."""

    def __init__(self, config: WorkdayConfig) -> None:
        self.config = config
        self.name = config.name
        self.description = f"Official {config.company} public Workday careers feed."

    @property
    def api_url(self) -> str:
        return f"https://{self.config.host}/wday/cxs/{self.config.tenant}/{self.config.site}/jobs"

    def search_page(self, query: str, offset: int) -> dict:
        with workday_access(self.api_url):
            response = httpx.post(
                self.api_url,
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query},
                timeout=settings.request_timeout_seconds,
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            )
            response.raise_for_status()
        return response.json()

    def posting_details(self, path: str) -> dict:
        url = self.api_url.removesuffix("/jobs") + path
        try:
            with workday_access(url):
                response = httpx.get(
                    url,
                    timeout=min(settings.request_timeout_seconds, 5),
                    headers={"User-Agent": settings.user_agent},
                    follow_redirects=True,
                )
                response.raise_for_status()
            payload = response.json()
            details = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
            return details if isinstance(details, dict) else {}
        except WorkdayRateLimited as exc:
            self.last_search_note = str(exc)
            return {}
        except (httpx.HTTPError, ValueError):
            logger.warning("Posting date unavailable for %s", url)
            return {}

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.can_confirm_absence = False
        self.scan_complete = False
        self.last_search_note = ""
        missing_locations = False
        jobs: list[JobPosting] = []
        seen_paths: set[str] = set()
        keywords = [keyword.casefold() for keyword in criteria.keywords]
        offset = 0
        while offset < 200:
            try:
                payload = self.search_page(self.config.search_text, offset)
            except WorkdayRateLimited as exc:
                if not jobs:
                    raise
                self.last_search_note = str(exc)
                return jobs
            postings = payload.get("jobPostings", [])
            total = int(payload["total"]) if "total" in payload else None
            if not postings:
                self.can_confirm_absence = total is not None and offset >= total
                break
            for item in postings:
                title = plain_text(item.get("title"))
                if not is_internship_role(title):
                    continue
                location = plain_text(item.get("locationsText"))
                path = str(item.get("externalPath") or "")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                details = self.posting_details(path)
                locations = [location, plain_text(details.get("location"))]
                for additional in details.get("additionalLocations") or []:
                    if isinstance(additional, str):
                        locations.append(additional)
                    elif isinstance(additional, dict):
                        locations.append(plain_text(additional.get("location")))
                location = "; ".join(dict.fromkeys(x for x in locations if x))
                if not details and "locations" in location.casefold():
                    missing_locations = True
                description = plain_text(details.get("jobDescription"))
                text = f"{title} {location} {description}".casefold()
                remote_type = plain_text(details.get("remoteType"))
                remote = remote_type.casefold() in {"remote", "remote only"}
                if keywords and not any(keyword in text for keyword in keywords):
                    continue
                if not relevant_to_krakow(location, "", remote=remote):
                    continue
                url = f"https://{self.config.host}/en-US/{self.config.site}{path}"
                posted = source_date(details.get("startDate"))
                evidence = date_evidence(
                    PublicationDate(posted, "Workday startDate", str(details.get("startDate")))
                    if posted
                    else PublicationDate(),
                    url,
                )
                jobs.append(
                    JobPosting(
                        source=self.name,
                        source_job_id=path.rsplit("_", 1)[-1],
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=location,
                        remote_type="Hybrid" if "hybrid" in remote_type.casefold() else "On-site",
                        employment_type=plain_text(details.get("timeType")) or "Not specified",
                        date_posted=posted,
                        description=description
                        or (
                            f"Official {self.config.company} listing. "
                            f"{title}. Location: {location}."
                        ),
                        raw_payload={**item, "_publication_check": evidence},
                    )
                )
            offset += len(postings)
            if total is not None and offset >= total:
                self.can_confirm_absence = True
                break
        self.can_confirm_absence = (
            self.can_confirm_absence and not missing_locations and not self.last_search_note
        )
        self.scan_complete = self.can_confirm_absence
        if not self.scan_complete and not self.last_search_note:
            self.last_search_note = "Result limit reached or multi-location details unavailable"
        return jobs
