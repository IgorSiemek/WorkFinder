from __future__ import annotations

import re
from contextlib import suppress
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.career_pages import ANCHOR_PATTERN, CareerPageSource
from app.scrapers.common import is_internship_role, plain_text, relevant_to_krakow
from app.scrapers.employer_feeds import matches
from app.scrapers.publication_dates import PublicationDate, date_evidence, source_date


class HaviDescription(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []
        self.root = ""
        self.finished = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self.depth and tag == self.root:
            self.depth += 1
        elif not self.depth and not self.finished and attributes.get("itemprop") == "description":
            self.root, self.depth = tag, 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag == self.root:
            self.depth -= 1
            self.finished = self.depth == 0

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)


class HaviSource(CareerPageSource):
    """Scan the official SAP careers table for Kraków across all result pages."""

    max_pages = 40

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        seen: set[str] = set()
        results = []
        offset = 0
        missing_city = False
        with httpx.Client(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            for _ in range(self.max_pages):
                response = client.get(
                    self.config.url,
                    params={
                        "q": "",
                        "locationsearch": "Krakow",
                        "startrow": offset,
                        "locale": "en_GB",
                    },
                )
                response.raise_for_status()
                html = response.text
                summary = plain_text(html)
                total_match = re.search(
                    r"Results\s+[\d,]+\s*[–-]\s*[\d,]+\s+of\s+([\d,]+)", summary
                )
                if total_match:
                    total = int(total_match[1].replace(",", ""))
                elif "there are currently no open positions matching" in summary.casefold():
                    total = 0
                else:
                    raise ValueError("HAVI careers table has no verifiable result count")
                page_urls: set[str] = set()
                for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.I | re.S):
                    anchors = [
                        match for match in ANCHOR_PATTERN.finditer(row) if "/job/" in match[1]
                    ]
                    if not anchors:
                        continue
                    anchor = anchors[0]
                    url = urljoin(self.config.url, anchor[1])
                    page_urls.add(url)
                    title = plain_text(anchor[2])
                    if url in seen or not is_internship_role(title):
                        continue
                    location = re.search(
                        r'<td\b[^>]*class=["\'][^"\']*colLocation[^"\']*["\'][^>]*>(.*?)</td>',
                        row,
                        re.I | re.S,
                    )
                    city = plain_text(location[1]) if location else ""
                    missing_city = missing_city or not city
                    if not relevant_to_krakow(city, "", remote=False):
                        continue
                    detail = client.get(url)
                    detail.raise_for_status()
                    parser = HaviDescription()
                    parser.feed(detail.text)
                    description = plain_text(" ".join(parser.parts))
                    if not description:
                        raise ValueError("HAVI job description is unavailable")
                    date_cell = re.search(
                        r'<td\b[^>]*class=["\'][^"\']*colDate[^"\']*["\'][^>]*>(.*?)</td>',
                        row,
                        re.I | re.S,
                    )
                    posted = None
                    evidence = PublicationDate()
                    if date_cell:
                        label = plain_text(date_cell[1])
                        with suppress(ValueError):
                            posted = source_date(
                                datetime.strptime(label, "%d %b %Y").date().isoformat()
                            )
                        if posted:
                            evidence = PublicationDate(posted, "HAVI job-date", label)
                    posting = JobPosting(
                        source=self.name,
                        source_job_id=url.rstrip("/").rsplit("/", 1)[-1],
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=city,
                        description=description,
                        date_posted=posted,
                        remote_type="Hybrid" if "hybrid" in description.casefold() else "On-site",
                        raw_payload={
                            "discovery": "official-havi-table",
                            "_publication_check": date_evidence(evidence, url),
                        },
                    )
                    if matches(posting, criteria):
                        results.append(posting)
                new_urls = page_urls - seen
                seen.update(page_urls)
                if len(seen) >= total:
                    self.scan_complete = self.can_confirm_absence = True
                    break
                if not new_urls:
                    break
                offset += len(page_urls)
        self.scan_complete = self.can_confirm_absence = self.scan_complete and not missing_city
        self.last_search_note = (
            "HAVI internship city metadata unavailable"
            if missing_city
            else ""
            if self.scan_complete
            else "HAVI pagination incomplete"
        )
        return results


class PepsiCoSource(CareerPageSource):
    """Read the public careers API used by PepsiCo's job search."""

    max_pages = 40

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        seen: set[str] = set()
        results = []
        missing_city = False
        origin = urlsplit(self.config.url)
        api_url = f"{origin.scheme}://{origin.netloc}/api/jobs"
        with httpx.Client(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            for page in range(1, self.max_pages + 1):
                response = client.get(
                    api_url,
                    params={
                        "location": "Krakow",
                        "limit": 100,
                        "page": page,
                        "lang": "en-us",
                    },
                )
                response.raise_for_status()
                data = response.json()
                if "totalCount" not in data or not isinstance(data.get("jobs"), list):
                    raise ValueError("PepsiCo careers API returned no valid result set")
                total = int(data["totalCount"])
                before = len(seen)
                for record in data["jobs"]:
                    item = record["data"]
                    identifier = str(item.get("req_id") or item["slug"])
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    locations = [item.get("full_location") or item.get("city") or ""]
                    derived = item.get("meta_data", {}).get("googlejobs", {}).get("derivedInfo", {})
                    for location in derived.get("locations", []):
                        locality = location.get("postalAddress", {}).get("locality")
                        if locality:
                            locations.append(locality)
                    city = "; ".join(dict.fromkeys(locations))
                    url = (
                        self.config.url.rstrip("/") + "/" + quote(str(item["slug"])) + "?lang=en-us"
                    )
                    posted = source_date(item.get("posted_date"))
                    evidence = (
                        PublicationDate(posted, "PepsiCo posted_date", str(item.get("posted_date")))
                        if posted
                        else PublicationDate()
                    )
                    description = plain_text(item.get("description"))
                    remote = bool(item.get("remote")) or item.get("location_type") == "REMOTE"
                    posting = JobPosting(
                        source=self.name,
                        source_job_id=identifier,
                        url=url,
                        title=plain_text(item.get("title")),
                        company=self.config.company,
                        location=city,
                        description=description,
                        requirements=plain_text(item.get("qualifications")),
                        responsibilities=plain_text(item.get("responsibilities")),
                        employment_type=" ".join(
                            [item.get("employment_type") or "", *(item.get("tags3") or [])]
                        ),
                        remote_type="Remote"
                        if remote
                        else "Hybrid"
                        if "hybrid" in description.casefold()
                        else "On-site",
                        date_posted=posted,
                        raw_payload={
                            "discovery": "official-pepsico-api",
                            "_publication_check": date_evidence(evidence, url),
                        },
                    )
                    if is_internship_role(
                        posting.title, posting.employment_type
                    ) and city.casefold() in {"", "poland", "multiple"}:
                        missing_city = True
                    if matches(posting, criteria):
                        results.append(posting)
                if len(seen) >= total:
                    self.scan_complete = self.can_confirm_absence = True
                    break
                if len(seen) == before:
                    break
        self.scan_complete = self.can_confirm_absence = self.scan_complete and not missing_city
        self.last_search_note = (
            "PepsiCo internship city metadata unavailable"
            if missing_city
            else ""
            if self.scan_complete
            else "PepsiCo pagination incomplete"
        )
        return results


class EuroclearSource(CareerPageSource):
    """Scan Euroclear's public Oracle job feed, preserving ambiguous city metadata."""

    max_pages = 20
    api_path = "/hcmRestApi/resources/11.13.18.05/"

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        del criteria  # The shared search service applies role, location and ignore rules.
        self.scan_complete = self.can_confirm_absence = False
        seen: set[str] = set()
        results = []
        unknown_city = 0
        origin = urlsplit(self.config.url)
        api = f"{origin.scheme}://{origin.netloc}" + self.api_path
        with httpx.Client(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            for page in range(self.max_pages):
                response = client.get(
                    api + "recruitingCEJobRequisitions",
                    params={
                        "onlyData": "true",
                        "expand": "requisitionList",
                        "finder": f"findReqs;siteNumber=CX_1003,limit=100,offset={page * 100}",
                    },
                )
                response.raise_for_status()
                groups = response.json().get("items", [])
                if len(groups) != 1 or "TotalJobsCount" not in groups[0]:
                    raise ValueError("Euroclear careers API returned no valid result count")
                group = groups[0]
                total = int(group["TotalJobsCount"])
                before = len(seen)
                for item in group["requisitionList"]:
                    identifier = str(item["Id"])
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    if not is_internship_role(item.get("Title", "")):
                        continue
                    detail = client.get(
                        api + "recruitingCEJobRequisitionDetails",
                        params={
                            "onlyData": "true",
                            "expand": "all",
                            "finder": f"ById;Id={identifier},siteNumber=CX_1003",
                        },
                    )
                    detail.raise_for_status()
                    details = detail.json().get("items", [])
                    if len(details) != 1:
                        raise ValueError("Euroclear job details are unavailable")
                    info = details[0]
                    locations = [info.get("PrimaryLocation") or ""]
                    for key in ("workLocation", "otherWorkLocations"):
                        locations.extend(
                            row["TownOrCity"] for row in info.get(key, []) if row.get("TownOrCity")
                        )
                    city = "; ".join(dict.fromkeys(locations))
                    country = info.get("PrimaryLocationCountry")
                    country_only = city.casefold() in {"", "poland", "polska"}
                    if (
                        country != "PL"
                        and city.casefold() not in {"poland", "polska"}
                        and not relevant_to_krakow(city, "", remote=False)
                    ):
                        continue
                    if country_only:
                        unknown_city += 1
                    url = self.config.url.rsplit("/jobs", 1)[0] + "/job/" + quote(identifier)
                    posted = source_date(item.get("PostedDate"))
                    evidence = (
                        PublicationDate(posted, "Oracle PostedDate", str(item.get("PostedDate")))
                        if posted
                        else PublicationDate()
                    )
                    workplace = str(info.get("WorkplaceType") or "").casefold()
                    results.append(
                        JobPosting(
                            source=self.name,
                            source_job_id=identifier,
                            url=url,
                            title=plain_text(info.get("Title")),
                            company=self.config.company,
                            location=city,
                            description=plain_text(info.get("ExternalDescriptionStr")),
                            requirements=plain_text(info.get("ExternalQualificationsStr")),
                            remote_type="Remote"
                            if workplace == "remote"
                            else "Hybrid"
                            if workplace == "hybrid"
                            else "On-site",
                            date_posted=posted,
                            raw_payload={
                                "discovery": "official-oracle-api",
                                "location_unverified": country_only,
                                "_publication_check": date_evidence(evidence, url),
                            },
                        )
                    )
                if len(seen) >= total:
                    self.scan_complete = True
                    break
                if len(seen) == before:
                    break
        self.scan_complete = self.scan_complete and unknown_city == 0
        self.can_confirm_absence = self.scan_complete
        self.last_search_note = (
            f"{unknown_city} Poland internships have no confirmed city"
            if unknown_city
            else ""
            if self.scan_complete
            else "Euroclear pagination incomplete"
        )
        return results
