from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.models import Application, ApplicationActivity, CandidateProfile, Job
from app.scrapers.base import JobPosting
from app.scrapers.common import is_senior_role
from app.services.search import import_posting


@dataclass(slots=True)
class ExcelImportResult:
    rows_read: int = 0
    new_jobs: int = 0
    updated_jobs: int = 0
    applications_created: int = 0
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)


HEADER_MAP = {
    "nazwa": "title",
    "firma": "company",
    "krotki opis": "description",
    "czy aplikowane": "applied",
    "etat": "employment_type",
    "data dodania": "date_added",
    "data aplikacji": "date_applied",
    "link": "url",
    "status": "status",
    "referal": "referral",
}

COMPANY_ALIASES = {
    "hiatchi": "Hitachi",
    "hitachi": "Hitachi",
    "abb": "ABB",
    "ey": "EY",
    "kpmg": "KPMG",
}


def _header(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    for fmt in ("%d.%m", "%d/%m"):
        try:
            parsed = datetime.strptime(text, fmt)
            return date(date.today().year, parsed.month, parsed.day)
        except ValueError:
            pass
    return None


def _valid_past_date(value: Any) -> date | None:
    parsed = _parse_date(value)
    return parsed if parsed and parsed <= date.today() else None


def _employment_type(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return f"{value.month}/{value.day} etatu"
    if isinstance(value, (float, int)) and 0 < value <= 1:
        common = {0.25: "1/4 etatu", 0.5: "1/2 etatu", 0.75: "3/4 etatu"}
        return common.get(float(value), f"{value:g} etatu")
    return _text(value) or "Not specified"


def _title_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return ""
    slug = unquote(parts[-1]).removesuffix(".html")
    title = re.sub(r"[-_]+", " ", slug)
    title = re.sub(r"\b(?:jr\d+|r\d{5,})\b", "", title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", title).strip().title()


def _company(value: Any) -> str:
    name = _text(value)
    return COMPANY_ALIASES.get(name.casefold(), name)


def _is_yes(value: Any) -> bool:
    return _text(value).casefold() in {"tak", "yes", "y", "true", "1"}


def _append_notes(existing: str, lines: list[str]) -> str:
    result = [line for line in existing.splitlines() if line.strip()]
    for line in lines:
        if line and line not in result:
            result.append(line)
    return "\n".join(result)


def _import_application(
    session: Session,
    job: Job,
    row: dict[str, Any],
    result: ExcelImportResult,
) -> None:
    if not _is_yes(row.get("applied")):
        return
    application = session.scalar(select(Application).where(Application.job_id == job.id))
    created = application is None
    if application is None:
        application = Application(job=job, status="Applied", application_url=job.original_url)
        session.add(application)
        session.flush()
        result.applications_created += 1
    elif application.status in {"Saved", "Preparing"}:
        application.status = "Applied"

    applied_date = _parse_date(row.get("date_applied"))
    if applied_date:
        application.date_applied = datetime.combine(applied_date, time.min)

    notes = []
    referral = _text(row.get("referral"))
    status = _text(row.get("status"))
    application.notes = "\n".join(
        line
        for line in application.notes.splitlines()
        if not line.startswith("Excel application date:")
    )
    if referral:
        notes.append(f"Excel referral: {referral}")
    if status:
        notes.append(f"Excel status: {status}")
    application.notes = _append_notes(application.notes, notes)

    if created:
        session.add(
            ApplicationActivity(
                application=application,
                event_type="status",
                description="Imported from Excel as Applied",
            )
        )


def import_excel_jobs(session: Session, workbook_path: str | Path) -> ExcelImportResult:
    path = Path(workbook_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise FileNotFoundError(f"Excel file not found: {path}")

    workbook = load_workbook(path, read_only=True, data_only=True)
    result = ExcelImportResult()
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        try:
            header_values = next(rows)
        except StopIteration as exc:
            raise ValueError("The Excel sheet is empty.") from exc

        columns = {
            index: HEADER_MAP[normalized]
            for index, value in enumerate(header_values)
            if (normalized := _header(value)) in HEADER_MAP
        }
        missing = {"title", "company", "url"} - set(columns.values())
        if missing:
            raise ValueError(f"Missing Excel columns: {', '.join(sorted(missing))}")

        profile = session.scalar(select(CandidateProfile).limit(1))
        for row_number, values in enumerate(rows, start=2):
            if not any(value is not None and _text(value) for value in values):
                continue
            result.rows_read += 1
            row = {key: values[index] for index, key in columns.items() if index < len(values)}
            url = _text(row.get("url"))
            company = _company(row.get("company"))
            title = _text(row.get("title")) or _title_from_url(url)
            if not url or not company or not title:
                result.skipped_rows += 1
                result.warnings.append(
                    f"Row {row_number} skipped: title, company, or link is missing."
                )
                continue
            if is_senior_role(title):
                result.skipped_rows += 1
                result.warnings.append(f"Row {row_number} skipped: senior-level title.")
                continue

            description = _text(row.get("description"))
            raw_payload = {
                "excel_file": path.name,
                "excel_sheet": sheet.title,
                "excel_row": row_number,
                **{key: _json_value(value) for key, value in row.items()},
            }
            posting = JobPosting(
                source="Excel",
                source_job_id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                url=url,
                title=title,
                company=company,
                location="Kraków",
                employment_type=_employment_type(row.get("employment_type")),
                description=description,
                date_posted=_valid_past_date(row.get("date_added")),
                raw_payload=raw_payload,
            )
            try:
                job, outcome = import_posting(session, posting, profile)
            except ValueError:
                result.skipped_rows += 1
                result.warnings.append(f"Row {row_number} skipped: invalid link.")
                continue

            if outcome == "new":
                result.new_jobs += 1
            else:
                result.updated_jobs += 1
            if posting.employment_type != "Not specified":
                job.employment_type = posting.employment_type
            if posting.date_posted and job.date_posted is None:
                job.date_posted = posting.date_posted

            job_notes = []
            referral = _text(row.get("referral"))
            status = _text(row.get("status"))
            raw_added_date = _text(row.get("date_added"))
            if referral:
                job_notes.append(f"Excel referral: {referral}")
            if status:
                job_notes.append(f"Excel status: {status}")
            if raw_added_date and posting.date_posted is None:
                job_notes.append(f"Excel date added: {raw_added_date} (preserved as entered)")
            job.notes = _append_notes(job.notes, job_notes)
            _import_application(session, job, row, result)

        session.commit()
        return result
    finally:
        workbook.close()
