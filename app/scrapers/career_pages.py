from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.scrapers.base import JobPosting, JobSource, SearchCriteria
from app.scrapers.common import plain_text

ANCHOR_PATTERN = re.compile(
    r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
KRAKOW_TERMS = ("kraków", "krakow", "cracow")
BLOCK_START_PATTERN = re.compile(r"<(?:li|tr|article|section)\b", re.IGNORECASE)
BLOCK_END_PATTERN = re.compile(r"</(?:li|tr|article|section)>", re.IGNORECASE)


def _nearby_html(html: str, start: int, end: int) -> str:
    starts = list(BLOCK_START_PATTERN.finditer(html, max(0, start - 4000), start))
    block_end = BLOCK_END_PATTERN.search(html, end, min(len(html), end + 4000))
    if starts and block_end:
        return html[starts[-1].start() : block_end.end()]
    return html[max(0, start - 700) : end + 700]


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
            if not title or absolute_url in seen:
                continue
            if not any(
                marker in absolute_url.casefold()
                for marker in ("job", "career", "pipeline", "project")
            ):
                continue
            nearby = plain_text(_nearby_html(html, match.start(), match.end()))
            combined = f"{title} {nearby}".casefold()
            if not self.config.trusted_location_filter and not any(
                term in combined for term in KRAKOW_TERMS
            ):
                continue
            if keywords and not any(keyword in combined for keyword in keywords):
                continue
            if len(title) < 4 or len(title) > 300:
                continue
            seen.add(absolute_url)
            source_id = hashlib.sha256(absolute_url.encode()).hexdigest()[:24]
            results.append(
                JobPosting(
                    source=self.name,
                    source_job_id=source_id,
                    url=absolute_url,
                    title=title,
                    company=self.config.company,
                    location="Kraków",
                    description=nearby[:4000],
                    raw_payload={"discovery": "official-career-page"},
                )
            )
            if len(results) >= 50:
                break
        return results
