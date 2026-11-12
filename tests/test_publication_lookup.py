from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from app.models import Job
from app.scrapers.publication_dates import (
    PublicationDate,
    extract_publication_date,
    lookup_publication_date,
)
from app.services import publication_dates

URL = "https://careers.hitachi.com/job/finance-intern"
TITLE = "Finance Intern"


def markup(value: object) -> str:
    return f'<script type="application/ld+json">{json.dumps(value)}</script>'


def test_jobposting_graph_uses_matching_offer_not_related_job_or_modified_date() -> None:
    html = markup(
        {
            "@graph": [
                {"@type": "Organization", "dateModified": "2026-10-16"},
                {
                    "@type": "JobPosting",
                    "url": "https://careers.hitachi.com/job/other",
                    "title": "Other Intern",
                    "datePosted": "2026-10-15",
                },
                {
                    "@type": ["Thing", "JobPosting"],
                    "url": URL,
                    "title": TITLE,
                    "datePosted": "2026-09-17T09:00:00Z",
                    "dateModified": "2026-10-16",
                },
            ]
        }
    )
    result = extract_publication_date(html, URL, TITLE)
    assert result.value == date(2026, 9, 17)
    assert result.method == "JobPosting datePosted"


@pytest.mark.parametrize(
    "html",
    [
        markup({"@type": "Article", "datePublished": "2026-09-17"}),
        '<meta property="article:modified_time" content="2026-09-17">',
        '<script type="application/ld+json">{bad json}</script>Copyright 2026',
        markup(
            {
                "@type": "JobPosting",
                "url": "https://careers.hitachi.com/job/other",
                "datePosted": "2026-09-17",
            }
        ),
        markup({"@type": "JobPosting", "url": URL, "datePosted": "2999-09-17"}),
        markup(
            [
                {"@type": "JobPosting", "url": URL, "datePosted": "2026-09-17"},
                {"@type": "JobPosting", "url": URL, "datePosted": "2026-09-18"},
            ]
        ),
    ],
)
def test_does_not_invent_dates_from_unrelated_or_conflicting_fields(html: str) -> None:
    assert extract_publication_date(html, URL, TITLE).value is None


def test_microdata_and_polish_publication_label() -> None:
    result = extract_publication_date(
        '<time itemprop="datePosted" datetime="2026-09-17"></time>',
        URL,
        TITLE,
    )
    assert result.value == date(2026, 9, 17)
    result = extract_publication_date(f"<h1>{TITLE}</h1>Data publikacji: 17.09.2026", URL, TITLE)
    assert result.value == date(2026, 9, 17)


def test_relative_label_is_estimated_and_open_ended_label_has_no_exact_date() -> None:
    result = extract_publication_date(f"<h1>{TITLE}</h1>Posted 29 Days Ago", URL, TITLE)
    assert result.value is None
    assert result.estimated == datetime.now(UTC).date() - timedelta(days=29)
    bound = extract_publication_date(f"<h1>{TITLE}</h1>Posted 30+ Days Ago", URL, TITLE)
    assert bound.value is None and bound.estimated is None
    assert bound.evidence == "Posted 30+ days ago"


def test_lookup_validates_redirect_before_fetching_next_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def get(url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(url)
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1/private"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("app.scrapers.publication_dates.httpx.get", get)
    result, info = lookup_publication_date(URL, TITLE)
    assert result.value is None and info["status"] == "failed"
    assert calls == [URL]


def test_workday_details_url_is_resolved_to_public_job_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def get(url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(url)
        return httpx.Response(
            200,
            json={"jobPostingInfo": {"startDate": "2026-09-17"}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.scrapers.publication_dates.httpx.get", get)
    result, info = lookup_publication_date(
        "https://motorolasolutions.wd5.myworkdayjobs.com/en-US/Careers/details/Finance-Intern_R1",
        TITLE,
    )
    assert result.value == date(2026, 9, 17)
    assert calls[0].endswith("/wday/cxs/motorolasolutions/Careers/job/Finance-Intern_R1")
    assert info["status"] == "verified"


def test_saved_date_check_keeps_original_date_and_caches_unknowns(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(
        source="Manual",
        title=TITLE,
        company="Example",
        location="Kraków",
        original_url=URL,
        canonical_url=URL,
        job_hash="publication-test",
    )
    session.add(job)
    session.commit()
    discovered = job.date_discovered
    today = datetime.now(UTC).date().isoformat()
    calls = []

    def lookup(url, title):  # noqa: ANN001, ANN202
        calls.append(url)
        return PublicationDate(), {"status": "not_found", "checked_on": today, "source_url": url}

    monkeypatch.setattr(publication_dates, "lookup_publication_date", lookup)
    assert publication_dates.refresh_posting_dates(session)["unknown"] == 1
    assert publication_dates.refresh_posting_dates(session) == {
        "verified": 0,
        "relative": 0,
        "unknown": 0,
    }
    assert len(calls) == 1
    publication_dates.save_date_evidence(
        job,
        {
            "status": "verified",
            "date": "2026-09-17",
            "method": "datePosted",
            "checked_on": today,
        },
    )
    publication_dates.save_date_evidence(
        job,
        {
            "status": "verified",
            "date": "2026-10-01",
            "method": "datePosted",
            "checked_on": today,
        },
    )
    session.commit()
    session.expire_all()
    assert job.date_posted == date(2026, 9, 17)
    assert job.date_discovered == discovered
    assert job.raw_payload["_publication_history"][-1]["date"] == "2026-09-17"


def test_offer_retry_saves_date_and_displays_source_evidence(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(
        source="Manual",
        title=TITLE,
        company="Example",
        location="Kraków",
        original_url=URL,
        canonical_url=URL,
        job_hash="date-retry",
    )
    session.add(job)
    session.commit()
    today = datetime.now(UTC).date().isoformat()

    def lookup(url, title):  # noqa: ANN001, ANN202
        return PublicationDate(date(2026, 9, 17)), {
            "status": "verified",
            "date": "2026-09-17",
            "checked_on": today,
            "method": "JobPosting datePosted",
            "evidence": "2026-09-17",
            "source_url": url,
        }

    monkeypatch.setattr(publication_dates, "lookup_publication_date", lookup)

    result = publication_dates.refresh_posting_dates(session, job_id=job.id, force=True)
    session.expire_all()
    assert result["verified"] == 1
    assert job.date_posted == date(2026, 9, 17)
    assert job.raw_payload["_publication_check"]["method"] == "JobPosting datePosted"
