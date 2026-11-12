from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, JobSource, SearchCriteria
from app.scrapers.common import is_internship_role, plain_text
from app.scrapers.publication_dates import lookup_publication_date

ANCHOR_PATTERN = re.compile(
    r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
KRAKOW_TERMS = ("kraków", "krakow", "cracow")
BLOCK_PATTERN = re.compile(r"<(/?)(li|tr|article|section)\b[^>]*>", re.IGNORECASE)


def _nearby_html(html: str, start: int, end: int) -> str:
    stack: list[tuple[str, int]] = []
    for token in BLOCK_PATTERN.finditer(html):
        closing, tag = token.groups()
        tag = tag.casefold()
        if not closing:
            stack.append((tag, token.start()))
        elif stack and stack[-1][0] == tag:
            _, block_start = stack.pop()
            if block_start <= start and token.end() >= end:
                return html[block_start : token.end()]
    return ""  # Nearby offers or a page-wide Kraków filter are not location evidence.


@dataclass(frozen=True, slots=True)
class CareerPageConfig:
    name: str
    company: str
    url: str
    trusted_location_filter: bool = False


class CareerPageSource(JobSource):
    """Conservative one-page monitor for an official public careers result page."""

    def __init__(self, config: CareerPageConfig) -> None:
        self.config = config
        self.name = config.name
        self.description = f"Official {config.company} Kraków careers page monitor."

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        response = httpx.get(
            self.config.url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        response.raise_for_status()
        html = response.text
        keywords = [keyword.casefold() for keyword in criteria.keywords]
        results: list[JobPosting] = []
        seen: set[str] = set()
        for match in ANCHOR_PATTERN.finditer(html):
            href, anchor_html = match.groups()
            title = plain_text(anchor_html)
            absolute_url = urljoin(str(response.url), href)
            if not title or absolute_url in seen or not is_internship_role(title):
                continue
            if not any(
                marker in absolute_url.casefold()
                for marker in ("job", "career", "pipeline", "project")
            ):
                continue
            card = _nearby_html(html, match.start(), match.end())
            # A container with several offers is not a location for this particular offer.
            if (
                len(
                    {
                        m[1]
                        for m in ANCHOR_PATTERN.finditer(card)
                        if is_internship_role(plain_text(m[2]))
                    }
                )
                > 1
            ):
                continue
            nearby = plain_text(card)
            combined = f"{title} {nearby}".casefold()
            if not any(term in nearby.casefold() for term in KRAKOW_TERMS):
                continue
            if keywords and not any(keyword in combined for keyword in keywords):
                continue
            if len(title) < 4 or len(title) > 300:
                continue
            seen.add(absolute_url)
            source_id = hashlib.sha256(absolute_url.encode()).hexdigest()[:24]
            posted, evidence = lookup_publication_date(absolute_url, title)
            results.append(
                JobPosting(
                    source=self.name,
                    source_job_id=source_id,
                    url=absolute_url,
                    title=title,
                    company=self.config.company,
                    location="Kraków",
                    description=nearby[:4000],
                    date_posted=posted.value,
                    raw_payload={
                        "discovery": "official-career-page",
                        "_publication_check": evidence,
                    },
                )
            )
            if len(results) >= 50:
                break
        return results
