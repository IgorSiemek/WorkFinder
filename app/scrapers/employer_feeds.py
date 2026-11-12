from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import quote, urljoin

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.career_pages import ANCHOR_PATTERN, CareerPageConfig, CareerPageSource
from app.scrapers.common import is_internship_role, plain_text, relevant_to_krakow
from app.scrapers.publication_dates import (
    PublicationDate,
    date_evidence,
    lookup_publication_date,
    source_date,
)


def embedded_json(html: str, marker: str) -> dict:
    """Read public SSR data without executing scripts or retaining session state."""
    if marker not in html:
        raise ValueError("Employer page no longer contains the expected public job data")
    payload, _ = json.JSONDecoder().raw_decode(html.split(marker, 1)[1].lstrip())
    if not isinstance(payload, dict):
        raise ValueError("Invalid employer job data")
    return payload


def matches(posting: JobPosting, criteria: SearchCriteria) -> bool:
    text = f"{posting.title} {posting.description}".casefold()
    return (
        is_internship_role(posting.title, posting.employment_type, posting.seniority)
        and relevant_to_krakow(posting.location, "", posting.remote_type == "Remote")
        and (not criteria.keywords or any(k.casefold() in text for k in criteria.keywords))
    )


class PhenomSource(CareerPageSource):
    """Read the same public, paginated job records rendered by Phenom career sites."""

    terms = ("intern", "trainee", "staż", "praktyk", "student", "apprentice", "placement")
    max_pages = 30

    def __init__(self, config: CareerPageConfig) -> None:
        super().__init__(config)
        self.description = f"Official {config.company} structured search with pagination."

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.can_confirm_absence = False  # Keyword indexes cannot prove an offer expired.
        self.scan_complete = True
        self.last_search_note = ""
        results: dict[str, JobPosting] = {}
        with httpx.Client(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            for term in self.terms:
                offset = 0
                seen: set[str] = set()
                for _ in range(self.max_pages):
                    response = client.get(
                        self.config.url, params={"keywords": term, "from": offset}
                    )
                    response.raise_for_status()
                    data = embedded_json(response.text, "phApp.ddo =")
                    search = data.get("eagerLoadRefineSearch", {})
                    if search.get("status") != 200 or "totalHits" not in search:
                        raise ValueError("Employer search did not return a valid result set")
                    records = search.get("data", {}).get("jobs", [])
                    total = int(search["totalHits"])
                    ids = {str(item.get("jobId") or item.get("jobSeqNo")) for item in records}
                    if not records or not (ids - seen):
                        if offset < total:
                            self.scan_complete = False
                        break
                    seen.update(ids)
                    for item in records:
                        posting = self.posting(item)
                        if matches(posting, criteria):
                            results[posting.source_job_id] = posting
                    offset += len(records)
                    if offset >= total:
                        break
                else:
                    self.scan_complete = False
        if not self.scan_complete:
            self.last_search_note = "Employer pagination incomplete or result limit reached"
        return list(results.values())

    def posting(self, item: dict) -> JobPosting:
        title = plain_text(item.get("title"))
        job_id = str(item.get("jobId") or item["jobSeqNo"])
        base = self.config.url.split("/search-results", 1)[0]
        slug = quote(re.sub(r"[^\w-]+", "-", title).strip("-"))
        url = item.get("applyUrl", "").removesuffix("/apply") or f"{base}/job/{job_id}/{slug}"
        location = "; ".join(
            dict.fromkeys([item.get("location", ""), *item.get("multi_location", [])])
        ).strip("; ")
        parser = item.get("ml_job_parser") or {}
        description = plain_text(
            parser.get("descriptionTeaser_ats") or item.get("descriptionTeaser")
        )
        remote = plain_text(item.get("RemoteType"))
        if not remote:
            pattern = re.search(
                r"working\s+pattern\s*:\s*(hybrid|remote|on.?site)", description, re.I
            )
            remote = pattern[1] if pattern else ""
        posted = source_date(item.get("postedDate"))
        pool = any(
            phrase in description.casefold()
            for phrase in (
                "this exact role may not be open",
                "this posting is to advertise potential job opportunities",
            )
        ) or any(
            phrase in title.casefold()
            for phrase in ("talent pool", "talent community", "future opportunities")
        )
        return JobPosting(
            source=self.name,
            source_job_id=job_id,
            url=url,
            title=title,
            company=self.config.company,
            location=location,
            remote_type="Remote"
            if remote.casefold() in {"remote", "remote only"}
            else "Hybrid"
            if "hybrid" in remote.casefold()
            else "On-site",
            employment_type=plain_text(item.get("type")),
            seniority=plain_text(item.get("requisitionType")),
            description=description,
            date_posted=posted,
            raw_payload={
                "discovery": "official-phenom-search",
                "jobId": job_id,
                "jobSeqNo": item.get("jobSeqNo"),
                "talent_pool": pool,
                "_publication_check": date_evidence(
                    PublicationDate(posted, "Employer postedDate", str(item.get("postedDate")))
                    if posted
                    else PublicationDate(),
                    url,
                ),
            },
        )


class HitachiSource(CareerPageSource):
    """Scan all Kraków jobs, including multi-location roles and later result pages."""

    max_pages = 40

    def __init__(self, config: CareerPageConfig) -> None:
        super().__init__(config)
        self.description = "Official Hitachi structured Kraków search across all result pages."

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.can_confirm_absence = False
        self.scan_complete = False
        results: list[JobPosting] = []
        seen: set[str] = set()
        with httpx.Client(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            for page in range(1, self.max_pages + 1):
                url = self.config.url.rstrip("/") + (f"/page/{page}" if page > 1 else "")
                response = client.get(url, params={"filter[city]": "Kraków"})
                response.raise_for_status()
                state = embedded_json(response.text, "window.__PRELOAD_STATE__ =")["jobSearch"]
                records = state["jobs"]
                total = int(state["totalJob"])
                ids = {str(item["requisitionID"]) for item in records}
                if not records or not (ids - seen):
                    self.scan_complete = len(seen) >= total
                    break
                seen.update(ids)
                for item in records:
                    title = plain_text(item.get("title"))
                    fields = {x["cfKey"]: x.get("value", "") for x in item.get("customFields", [])}
                    seniority = plain_text(fields.get("cf_experience"))
                    if not is_internship_role(title, seniority=seniority):
                        continue
                    location = "; ".join(
                        x.get("locationText") or x.get("city", "")
                        for x in item.get("locations", [])
                    )
                    remote = plain_text(fields.get("cf_remote_type"))
                    posting = JobPosting(
                        source=self.name,
                        source_job_id=str(item["requisitionID"]),
                        url=urljoin(self.config.url + "/", "/" + item["originalURL"].lstrip("/")),
                        title=title,
                        company=self.config.company,
                        location=location,
                        seniority=seniority,
                        employment_type=", ".join(item.get("employmentType", [])),
                        remote_type="Remote"
                        if item.get("isRemote") or remote.casefold() == "remote"
                        else "Hybrid"
                        if "hybrid" in remote.casefold()
                        else "On-site",
                        description=f"{title}. {location}. {seniority}.",
                        raw_payload={
                            "discovery": "official-hitachi-search",
                            "requisitionID": item["requisitionID"],
                        },
                    )
                    if matches(posting, criteria):
                        posted, evidence = lookup_publication_date(posting.url, title)
                        posting.date_posted = posted.value
                        posting.raw_payload["_publication_check"] = evidence
                        results.append(posting)
                if len(seen) >= total:
                    self.scan_complete = True
                    break
        self.can_confirm_absence = self.scan_complete
        self.last_search_note = "" if self.scan_complete else "Employer pagination incomplete"
        return results


class AliorSource(CareerPageSource):
    max_pages = 40

    def __init__(self, config: CareerPageConfig) -> None:
        super().__init__(config)
        self.description = "Official Alior offer table with pagination, city and publication dates."

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.can_confirm_absence = False
        self.scan_complete = False
        results: list[JobPosting] = []
        seen: set[str] = set()
        with httpx.Client(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            page_count = 1
            for page in range(1, self.max_pages + 1):
                response = client.get(self.config.url, params={"page": page})
                response.raise_for_status()
                html = response.text
                pages = [int(x) for x in re.findall(r"[?&]page=(\d+)", html)]
                page_count = max([page_count, *pages])
                rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.I | re.S)
                page_ids: set[str] = set()
                for row in rows:
                    anchors = [m for m in ANCHOR_PATTERN.finditer(row) if "/oferta/" in m[1]]
                    if not anchors:
                        continue
                    anchor = anchors[0]
                    url = urljoin(self.config.url, anchor[1])
                    page_ids.add(url)
                    if url in seen:
                        continue
                    title = plain_text(anchor[2])
                    location = re.search(
                        r'<td\b[^>]*class=["\'][^"\']*job-location[^"\']*["\'][^>]*>(.*?)</td>',
                        row,
                        re.I | re.S,
                    )
                    posting = JobPosting(
                        source=self.name,
                        source_job_id=url.rsplit("/", 1)[-1],
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=plain_text(location[1]) if location else "",
                        description=plain_text(row),
                        raw_payload={"discovery": "official-alior-table"},
                    )
                    if matches(posting, criteria):
                        date_cell = re.search(
                            r'<td\b[^>]*class=["\'][^"\']*job-date[^"\']*["\'][^>]*>(.*?)</td>',
                            row,
                            re.I | re.S,
                        )
                        if date_cell:
                            try:
                                value = datetime.strptime(plain_text(date_cell[1]), "%d.%m.%Y")
                                posting.date_posted = source_date(value.date().isoformat())
                            except ValueError:
                                pass
                        posting.raw_payload["_publication_check"] = date_evidence(
                            PublicationDate(
                                posting.date_posted, "Employer job-date", plain_text(date_cell[1])
                            )
                            if posting.date_posted and date_cell
                            else PublicationDate(),
                            url,
                        )
                        results.append(posting)
                if not page_ids or not (page_ids - seen):
                    break
                seen.update(page_ids)
                if page >= page_count:
                    self.scan_complete = True
                    break
        self.can_confirm_absence = self.scan_complete
        self.last_search_note = "" if self.scan_complete else "Employer pagination incomplete"
        return results
