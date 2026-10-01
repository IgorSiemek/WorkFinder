from __future__ import annotations

from datetime import date, datetime, timedelta

from openpyxl import Workbook, load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationActivity, Job
from app.services.excel_import import import_excel_jobs
from app.services.excel_sync import mark_applied_in_excel


def test_excel_import_is_idempotent(tmp_path, session: Session) -> None:  # noqa: ANN001
    path = tmp_path / "jobs.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "Nazwa",
            "Firma",
            "Krotki opis",
            "Czy aplikowane",
            "Etat",
            "Data dodania",
            "Data aplikacji",
            "Link",
            "Status",
            "Referal?",
        ]
    )
    sheet.append(
        [
            "Finance Intern",
            "Hiatchi",
            "Excel and reporting",
            "tak",
            "30h",
            date(2025, 5, 1),
            date.today() + timedelta(days=30),
            "https://example.com/jobs/finance-intern?utm_source=sheet",
            "x",
            "Tak moge zalatwic",
        ]
    )
    sheet.append(
        [
            "Finance Intern",
            "Hitachi",
            "Excel and reporting",
            "tak",
            "30h",
            date(2025, 5, 1),
            date.today() + timedelta(days=30),
            "https://example.com/jobs/finance-intern?utm_source=sheet",
            "x",
            "Tak moge zalatwic",
        ]
    )
    workbook.save(path)
    workbook.close()

    first = import_excel_jobs(session, path)
    second = import_excel_jobs(session, path)

    assert (first.rows_read, first.new_jobs, first.updated_jobs) == (2, 1, 1)
    assert first.applications_created == 1
    assert (second.new_jobs, second.updated_jobs, second.applications_created) == (0, 2, 0)
    assert session.scalar(select(func.count(Job.id))) == 1
    assert session.scalar(select(func.count(Application.id))) == 1
    assert session.scalar(select(func.count(ApplicationActivity.id))) == 1
    job = session.scalar(select(Job))
    application = session.scalar(select(Application))
    assert job is not None and job.company == "Hitachi" and job.location == "Kraków"
    assert application is not None and application.status == "Applied"
    assert application.date_applied is not None
    assert application.date_applied.date() == date.today() + timedelta(days=30)
    assert "Excel referral: Tak moge zalatwic" in application.notes


def test_mark_applied_updates_matching_link_or_appends_row(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "jobs.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "Nazwa",
            "Firma",
            "Krotki opis",
            "Czy aplikowane",
            "Etat",
            "Data dodania",
            "Data aplikacji",
            "Link",
            "Status",
            "Referal?",
        ]
    )
    sheet.append(
        [
            "Existing",
            "Example",
            "",
            "nie",
            "30h",
            None,
            None,
            "https://example.com/job/1?utm_source=excel",
            "",
            "",
        ]
    )
    workbook.save(path)
    workbook.close()

    existing = Job(
        source="Test",
        source_job_id="1",
        canonical_url="https://example.com/job/1",
        original_url="https://example.com/job/1",
        title="Existing",
        company="Example",
        location="Kraków",
        description="Existing role",
        employment_type="30h",
        job_hash="one",
        date_discovered=datetime(2026, 9, 30),
    )
    updated = mark_applied_in_excel(path, existing, date(2026, 10, 1))
    assert not updated.created and updated.row_number == 2

    new = Job(
        source="Test",
        source_job_id="2",
        canonical_url="https://example.com/job/2",
        original_url="https://example.com/job/2",
        title="New Role",
        company="New Company",
        location="Kraków",
        description="New description",
        employment_type="Internship",
        job_hash="two",
        date_discovered=datetime(2026, 10, 1),
    )
    appended = mark_applied_in_excel(path, new, date(2026, 10, 1))
    assert appended.created and appended.row_number == 3

    result = load_workbook(path, read_only=True, data_only=True)
    rows = list(result.active.iter_rows(values_only=True))
    result.close()
    assert rows[1][3] == "tak"
    assert rows[1][6].date() == date(2026, 10, 1)
    assert rows[2][0] == "New Role"
    assert rows[2][1] == "New Company"
    assert rows[2][3] == "tak"
    assert rows[2][7] == "https://example.com/job/2"
