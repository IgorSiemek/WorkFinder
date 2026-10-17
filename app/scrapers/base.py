from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(slots=True)
class SearchCriteria:
    keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=lambda: ["Kraków", "Cracow"])
    remote_allowed: bool = False


@dataclass(slots=True)
class JobPosting:
    source: str
    source_job_id: str | None
    url: str
    title: str
    company: str
    location: str = ""
    country: str = "Poland"
    remote_type: str = "On-site"
    employment_type: str = "Not specified"
    seniority: str = "Not specified"
    description: str = ""
    requirements: str = ""
    responsibilities: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    date_posted: date | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


class JobSource(ABC):
    name: str
    description: str
    enabled: bool = True
    can_confirm_absence: bool = False

    @abstractmethod
    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        """Return public job postings matching the criteria."""
