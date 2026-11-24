from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.career_pages import ANCHOR_PATTERN, CareerPageSource
from app.scrapers.common import is_internship_role, is_talent_pool, plain_text, relevant_to_krakow
from app.scrapers.early_careers import public_client
from app.scrapers.employer_feeds import matches
from app.scrapers.publication_dates import date_evidence, extract_publication_date


class DeloitteSource(CareerPageSource):
    """Scan Deloitte's public Poland results with per-offer location metadata."""

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        self.last_search_note = "Deloitte search incomplete"
        seen: set[str] = set()
        results: list[JobPosting] = []
        missing_city = False
        offset = 0
        with public_client() as client:
            for _ in range(30):
                response = client.get(
                    self.config.url,
                    params={
                        "523": "[5515]",  # Poland on Deloitte Central Europe's search form
                        "523_format": "1482",
                        "listFilterMode": "1",
                        "jobRecordsPerPage": 10,
                        "jobOffset": offset,
                    },
                )
                response.raise_for_status()
                bounds = re.search(
                    r"Displaying\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)", plain_text(response.text)
                )
                cards = re.findall(
                    r'<article\b[^>]*class="[^"]*\barticle--result\b[^"]*"[^>]*>(.*?)</article>',
                    response.text,
                    re.S,
                )
                if not bounds:
                    raise ValueError("Deloitte search has no verifiable pagination")
                start, end, total = map(int, bounds.groups())
                if start != offset + 1 or end - offset != len(cards):
                    break
                for card in cards:
                    anchor = next(
                        (
                            link
                            for link in ANCHOR_PATTERN.finditer(card)
                            if "/JobDetail/" in link[1]
                        ),
                        None,
                    )
                    if not anchor:
                        raise ValueError("Deloitte job link is unavailable")
                    url = urljoin(self.config.url, anchor[1])
                    if urlsplit(url).netloc != urlsplit(self.config.url).netloc:
                        raise ValueError("Unexpected Deloitte job host")
                    identifier = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    title = plain_text(anchor[2])
                    if not is_internship_role(title):
                        continue
                    subtitle = re.search(
                        r'<div class="article__header__text__subtitle">(.*?)</div>', card, re.S
                    )
                    fields = (
                        re.findall(r"<span\b[^>]*>(.*?)</span>", subtitle[1], re.S)
                        if subtitle
                        else []
                    )
                    city = plain_text(fields[0]) if fields else ""
                    missing_city = missing_city or not city or city.casefold() == "poland"
                    if not relevant_to_krakow(city, "", remote=False):
                        continue
                    detail = client.get(url)
                    if detail.status_code in {404, 410}:
                        continue
                    detail.raise_for_status()
                    body = re.search(
                        r"<article\b[^>]*\bicon-description\b[^>]*>(.*?)</article>",
                        detail.text,
                        re.S,
                    )
                    if not body:
                        missing_city = True
                        continue
                    description = plain_text(body[1])
                    published = extract_publication_date(detail.text, url, title)
                    job = JobPosting(
                        source=self.name,
                        source_job_id=identifier,
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=city,
                        description=description,
                        remote_type="Hybrid"
                        if any(plain_text(f) == "Hybrid" for f in fields)
                        else "On-site",
                        date_posted=published.value,
                        raw_payload={
                            "talent_pool": is_talent_pool(title, description),
                            "_publication_check": date_evidence(published, url),
                        },
                    )
                    if matches(job, criteria):
                        results.append(job)
                if end >= total:
                    self.scan_complete = self.can_confirm_absence = (
                        len(seen) == total and not missing_city
                    )
                    break
                offset = end
        if self.scan_complete:
            self.last_search_note = ""
        return results
