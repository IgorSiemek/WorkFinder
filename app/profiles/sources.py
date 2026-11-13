from __future__ import annotations

import httpx

from app.config import settings
from app.profiles.scope import active_scope, location_matches, normalized_location, role_matches
from app.scrapers.arbeitnow import ArbeitnowSource
from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.common import plain_text
from app.scrapers.publication_dates import PublicationDate, date_evidence, source_date
from app.scrapers.workday import WorkdaySource


class PersonalWorkdaySource(WorkdaySource):
    """A separate search path for configurable profiles; legacy searches stay unchanged."""

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        scope = active_scope()
        queries = [
            self.config.search_text
            if normalized_location(city) == "krakow"
            else normalized_location(city)
            for city in scope["locations"]
        ]
        if scope["remote_allowed"]:
            queries.extend(scope["titles"] or [""])
        self.scan_complete = self.can_confirm_absence = False
        complete = True
        seen = set()
        jobs = []
        for query in dict.fromkeys(queries):
            offset, query_complete = 0, False
            query_paths = set()
            while offset < 200:
                response = httpx.post(
                    self.api_url,
                    json={
                        "appliedFacets": {},
                        "limit": 20,
                        "offset": offset,
                        "searchText": query,
                    },
                    timeout=settings.request_timeout_seconds,
                    headers={"User-Agent": settings.user_agent},
                    follow_redirects=True,
                )
                response.raise_for_status()
                data = response.json()
                records = data.get("jobPostings")
                if not isinstance(records, list) or "total" not in data:
                    raise ValueError("Workday returned no verifiable result set")
                total = int(data["total"])
                if not records:
                    query_complete = offset >= total
                    break
                page_paths = {
                    item.get("externalPath") for item in records if item.get("externalPath")
                }
                new_paths = page_paths - query_paths
                query_paths.update(page_paths)
                if not new_paths:
                    break
                for item in records:
                    title = plain_text(item.get("title"))
                    if not role_matches(title):
                        continue
                    path = item.get("externalPath") or ""
                    if not path.startswith("/job/"):
                        complete = False
                        continue
                    if path in seen:
                        continue
                    seen.add(path)
                    details = self.posting_details(path)
                    locations = [
                        plain_text(item.get("locationsText")),
                        plain_text(details.get("location")),
                    ]
                    for other in details.get("additionalLocations") or []:
                        locations.append(
                            plain_text(other if isinstance(other, str) else other.get("location"))
                        )
                    location = "; ".join(dict.fromkeys(value for value in locations if value))
                    remote_type = plain_text(details.get("remoteType"))
                    remote = remote_type.casefold() in {"remote", "remote only"}
                    if not details:
                        complete = (
                            False  # Details can contain additional locations and remote status.
                        )
                    if not location_matches(location, remote=remote):
                        continue
                    description = plain_text(details.get("jobDescription"))
                    text = f"{title} {description}".casefold()
                    if criteria.keywords and not any(
                        k.casefold() in text for k in criteria.keywords
                    ):
                        continue
                    url = f"https://{self.config.host}/en-US/{self.config.site}{path}"
                    posted = source_date(details.get("startDate"))
                    evidence = (
                        PublicationDate(posted, "Workday startDate", str(details.get("startDate")))
                        if posted
                        else PublicationDate()
                    )
                    jobs.append(
                        JobPosting(
                            source=self.name,
                            source_job_id=path.rsplit("_", 1)[-1],
                            url=url,
                            title=title,
                            company=self.config.company,
                            location=location,
                            description=description or title,
                            date_posted=posted,
                            employment_type=plain_text(details.get("timeType")) or "Not specified",
                            remote_type="Remote"
                            if remote
                            else "Hybrid"
                            if "hybrid" in remote_type.casefold()
                            else "On-site",
                            raw_payload={
                                **item,
                                "_publication_check": date_evidence(evidence, url),
                            },
                        )
                    )
                offset += len(records)
                if offset >= total:
                    query_complete = len(query_paths) >= total
                    break
            complete = complete and query_complete
        self.scan_complete = self.can_confirm_absence = complete
        self.last_search_note = (
            "" if complete else "Result limit reached or job details unavailable"
        )
        return jobs


class PersonalArbeitnowSource(ArbeitnowSource):
    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        response = httpx.get(
            self.api_url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        response.raise_for_status()
        jobs = []
        for item in response.json().get("data", []):
            title = plain_text(item.get("title"))
            employment = ", ".join(item.get("job_types") or [])
            location = plain_text(item.get("location"))
            remote = bool(item.get("remote"))
            description = plain_text(item.get("description"))
            if not role_matches(title, employment) or not location_matches(location, remote=remote):
                continue
            if criteria.keywords and not any(
                k.casefold() in f"{title} {description}".casefold() for k in criteria.keywords
            ):
                continue
            jobs.append(
                JobPosting(
                    source=self.name,
                    source_job_id=str(item.get("slug") or item["url"]),
                    url=item["url"],
                    title=title,
                    company=plain_text(item.get("company_name")) or "Unknown",
                    location=location,
                    remote_type="Remote" if remote else "On-site",
                    employment_type=employment,
                    description=description,
                    date_posted=source_date(item.get("created_at")),
                    raw_payload=item,
                )
            )
        return jobs
