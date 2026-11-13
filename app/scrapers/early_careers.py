from __future__ import annotations

import json
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
from app.scrapers.publication_dates import (
    DateMarkup,
    PublicationDate,
    date_evidence,
    extract_publication_date,
    job_nodes,
    source_date,
)


class JobDescription(HTMLParser):
    """Collect all SAP description sections, or the iCIMS job body."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ""
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if self.depth and tag == self.root:
            self.depth += 1
        elif not self.depth and (
            attributes.get("itemprop") == "description"
            or "iCIMS_InfoMsg_Job" in (attributes.get("class") or "").split()
        ):
            self.root, self.depth = tag, 1

    def handle_endtag(self, tag):
        if self.depth and tag == self.root:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def public_client() -> httpx.Client:
    return httpx.Client(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )


def job_details(client: httpx.Client, url: str, title: str) -> tuple[str, PublicationDate, bool]:
    response = client.get(url, params={"in_iframe": "1"} if "icims.com" in url else None)
    response.raise_for_status()
    markup = JobDescription()
    markup.feed(response.text)
    description = plain_text(" ".join(markup.parts))
    dates = DateMarkup()
    dates.feed(response.text)
    remote = False
    for script in dates.scripts:
        with suppress(ValueError, RecursionError):
            for node in job_nodes(json.loads(script)):
                if plain_text(node.get("title")).casefold() == title.casefold():
                    description = description or plain_text(node.get("description"))
                    remote = str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    if not description:
        raise ValueError("Official job description is unavailable")
    evidence = extract_publication_date(response.text, url, title)
    if not evidence.value:
        for value in dates.dates:
            with suppress(ValueError):
                posted = source_date(
                    datetime.strptime(value, "%a %b %d %H:%M:%S %Z %Y").date().isoformat()
                )
                if posted:
                    evidence = PublicationDate(posted, "SAP datePosted markup", value)
    return description, evidence, remote


def posting(source, client, identifier, title, city, url, *, date_label="") -> JobPosting:
    description, evidence, remote = job_details(client, url, title)
    if not evidence.value and date_label:
        with suppress(ValueError):
            posted = source_date(datetime.strptime(date_label, "%m/%d/%y").date().isoformat())
            if posted:
                evidence = PublicationDate(posted, "SAP unifiedStandardStart", date_label)
    return JobPosting(
        source=source.name,
        source_job_id=identifier,
        url=url,
        title=title,
        company=source.config.company,
        location=city,
        description=description,
        date_posted=evidence.value,
        remote_type="Remote"
        if remote
        else "Hybrid"
        if "hybrid" in description.casefold()
        else "On-site",
        raw_payload={
            "discovery": "official-careers-feed",
            "_publication_check": date_evidence(evidence, url),
        },
    )


class LundbeckSource(CareerPageSource):
    """Read the official SAP careers table, including all Krakow result pages."""

    max_pages = 40

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        results = []
        seen = set()
        missing_city = False
        offset = 0
        with public_client() as client:
            for _ in range(self.max_pages):
                response = client.get(
                    self.config.url,
                    params={
                        "q": "",
                        "locationsearch": "Krakow",
                        "locale": "en_GB",
                        "startrow": offset,
                    },
                )
                response.raise_for_status()
                html = response.text
                summary = plain_text(html)
                count = re.search(r"Results\s+[\d,]+\s*[\u2013-]\s*[\d,]+\s+of\s+([\d,]+)", summary)
                if count:
                    total = int(count[1].replace(",", ""))
                elif "there are currently no open positions matching" in summary.casefold():
                    total = 0
                else:
                    raise ValueError("Lundbeck careers table has no verifiable result count")
                page_urls = set()
                for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.I | re.S):
                    link = next((m for m in ANCHOR_PATTERN.finditer(row) if "/job/" in m[1]), None)
                    if not link:
                        continue
                    url = urljoin(self.config.url, link[1])
                    if urlsplit(url).hostname != urlsplit(self.config.url).hostname:
                        raise ValueError("Unexpected Lundbeck job host")
                    page_urls.add(url)
                    title = plain_text(link[2])
                    if url in seen or not is_internship_role(title):
                        continue
                    location = re.search(
                        r'<td\b[^>]*class="[^"\n]*colLocation[^"\n]*"[^>]*>(.*?)</td>',
                        row,
                        re.I | re.S,
                    )
                    city = plain_text(location[1]) if location else ""
                    missing_city = missing_city or not city or city.casefold() == "poland"
                    if not relevant_to_krakow(city, "", remote=False):
                        continue
                    identifier = url.rstrip("/").rsplit("/", 1)[-1]
                    job = posting(self, client, identifier, title, city, url)
                    if matches(job, criteria):
                        results.append(job)
                new_urls = page_urls - seen
                seen.update(page_urls)
                if len(seen) >= total:
                    self.scan_complete = True
                    break
                if not new_urls:
                    break
                offset += len(page_urls)
        self.scan_complete = self.can_confirm_absence = self.scan_complete and not missing_city
        self.last_search_note = (
            ""
            if self.scan_complete
            else "Lundbeck pagination incomplete or internship city unavailable"
        )
        return results


class TEConnectivitySource(CareerPageSource):
    """Read and paginate the public SAP job search used by TE's careers page."""

    max_pages = 40

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        seen = set()
        results = []
        missing_city = False
        origin = "https://" + urlsplit(self.config.url).netloc
        with public_client() as client:
            response = client.get(self.config.url)
            response.raise_for_status()
            token = re.search(r'"X-CSRF-Token"\s*:\s*"([^"]+)"', response.text)
            headers = {"X-CSRF-Token": token[1]} if token else {}
            for page in range(self.max_pages):
                response = client.post(
                    origin + "/services/recruiting/v1/jobs",
                    json={
                        "keywords": "",
                        "locale": "en_US",
                        "location": "Krakow",
                        "pageNumber": page,
                        "sortBy": "recent",
                    },
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                records = data.get("jobSearchResult", [])
                if "totalJobs" not in data or not isinstance(records, list):
                    raise ValueError("TE careers API has no valid result set")
                total = int(data["totalJobs"])
                if "jobSearchResult" not in data and total and not seen:
                    raise ValueError("TE careers API has no valid result set")
                before = len(seen)
                for record in records:
                    item = record["response"]
                    identifier = str(item["id"])
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    title = plain_text(item.get("unifiedStandardTitle"))
                    if not is_internship_role(title):
                        continue
                    city = plain_text("; ".join(item.get("jobLocationShort") or []))
                    missing_city = missing_city or not city or city.casefold() == "poland"
                    if not relevant_to_krakow(city, "", remote=False):
                        continue
                    slug = quote(item.get("unifiedUrlTitle") or title, safe="%")
                    url = f"{origin}/job/{slug}/{quote(identifier, safe='')}-en_US/"
                    job = posting(
                        self,
                        client,
                        identifier,
                        title,
                        city,
                        url,
                        date_label=item.get("unifiedStandardStart") or "",
                    )
                    if matches(job, criteria):
                        results.append(job)
                if len(seen) >= total:
                    self.scan_complete = True
                    break
                if len(seen) == before:
                    break
        self.scan_complete = self.can_confirm_absence = self.scan_complete and not missing_city
        self.last_search_note = (
            "" if self.scan_complete else "TE pagination incomplete or internship city unavailable"
        )
        return results


class StoneXSource(CareerPageSource):
    """Scan the official international iCIMS portal, checking each offer's city."""

    max_pages = 40

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        seen = set()
        results = []
        missing_city = False
        with public_client() as client:
            for page in range(self.max_pages):
                response = client.get(
                    self.config.url, params={"ss": "1", "in_iframe": "1", "pr": page}
                )
                response.raise_for_status()
                html = response.text
                summary = plain_text(html)
                pages = re.search(r"\bPage\s+(\d+)\s+of\s+(\d+)\b", summary)
                if not pages:
                    if "there are currently no job openings" in summary.casefold():
                        self.scan_complete = True
                        break
                    raise ValueError("StoneX search has no verifiable pagination")
                if int(pages[1]) != page + 1:
                    break
                cards = re.findall(
                    r'<li\b[^>]*class="iCIMS_JobCardItem"[^>]*>(.*?)</li>', html, re.I | re.S
                )
                if not cards:
                    raise ValueError("StoneX search has no job cards")
                before = len(seen)
                for card in cards:
                    link = next(
                        (
                            m
                            for m in ANCHOR_PATTERN.finditer(card)
                            if re.search(r"/jobs/\d+/", m[1])
                        ),
                        None,
                    )
                    if not link:
                        raise ValueError("StoneX job link is unavailable")
                    url = urljoin(self.config.url, link[1]).split("?", 1)[0]
                    if urlsplit(url).netloc != urlsplit(self.config.url).netloc:
                        raise ValueError("Unexpected StoneX job host")
                    identifier = re.search(r"/jobs/(\d+)/", url)[1]
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    heading = re.search(r"<h3\b[^>]*>(.*?)</h3>", link[2], re.I | re.S)
                    title = plain_text(heading[1]) if heading else ""
                    if not title:
                        raise ValueError("StoneX job title is unavailable")
                    if not is_internship_role(title):
                        continue
                    location = re.search(
                        r"Job Locations</span>\s*<span[^>]*>(.*?)</span>", card, re.I | re.S
                    )
                    city = plain_text(location[1]) if location else ""
                    missing_city = missing_city or not city or city.casefold() == "poland"
                    if not relevant_to_krakow(city, "", remote=False):
                        continue
                    job = posting(self, client, identifier, title, city, url)
                    if matches(job, criteria):
                        results.append(job)
                if len(seen) == before:
                    break
                if page + 1 >= int(pages[2]):
                    self.scan_complete = True
                    break
        self.scan_complete = self.can_confirm_absence = self.scan_complete and not missing_city
        self.last_search_note = (
            ""
            if self.scan_complete
            else "StoneX pagination incomplete or internship city unavailable"
        )
        return results
