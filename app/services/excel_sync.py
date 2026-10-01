from __future__ import annotations

import os
from copy import copy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

from openpyxl import load_workbook

from app.config import PROJECT_ROOT
from app.models import Job
from app.services.excel_import import HEADER_MAP, _header, _text
from app.services.normalization import canonicalize_url


@dataclass(frozen=True, slots=True)
class ExcelApplicationSync:
    row_number: int
    created: bool


def _workbook_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _copy_row_style(sheet, source_row: int, target_row: int) -> None:  # noqa: ANN001
    if source_row < 2:
        return
    for column in range(1, sheet.max_column + 1):
        source = sheet.cell(source_row, column)
        target = sheet.cell(target_row, column)
        if source.has_style:
            target._style = copy(source._style)  # noqa: SLF001
        if source.number_format:
            target.number_format = source.number_format
    sheet.row_dimensions[target_row].height = sheet.row_dimensions[source_row].height


def mark_applied_in_excel(
    workbook_path: str | Path,
    job: Job,
    applied_on: date,
) -> ExcelApplicationSync:
    path = _workbook_path(workbook_path)
    if not path.is_file():
        raise FileNotFoundError(f"Excel file not found: {path}")

    workbook = load_workbook(path, keep_vba=path.suffix.casefold() == ".xlsm")
    temp_path = path.with_name(f".{path.stem}.{uuid4().hex}.tmp{path.suffix}")
    try:
        sheet = workbook.active
        columns = {
            HEADER_MAP[normalized]: cell.column
            for cell in sheet[1]
            if (normalized := _header(cell.value)) in HEADER_MAP
        }
        missing = {"url", "applied", "date_applied"} - set(columns)
        if missing:
            raise ValueError(f"Missing Excel columns: {', '.join(sorted(missing))}")

        target_url = canonicalize_url(job.original_url)
        row_number = 0
        for candidate in range(2, sheet.max_row + 1):
            value = _text(sheet.cell(candidate, columns["url"]).value)
            if not value:
                continue
            try:
                matches = canonicalize_url(value) == target_url
            except ValueError:
                matches = value == job.original_url
            if matches:
                row_number = candidate
                break

        created = row_number == 0
        if created:
            row_number = sheet.max_row + 1
            _copy_row_style(sheet, row_number - 1, row_number)
            values = {
                "title": job.title,
                "company": job.company,
                "description": job.description,
                "employment_type": job.employment_type,
                "date_added": job.date_discovered.date(),
                "url": job.original_url,
            }
            for key, value in values.items():
                if key in columns:
                    sheet.cell(row_number, columns[key], value)

        sheet.cell(row_number, columns["applied"], "tak")
        applied_cell = sheet.cell(row_number, columns["date_applied"], applied_on)
        applied_cell.number_format = "dd.mm.yyyy"
        if created and "date_added" in columns:
            sheet.cell(row_number, columns["date_added"]).number_format = "dd.mm.yyyy"

        workbook.save(temp_path)
        workbook.close()
        os.replace(temp_path, path)
        return ExcelApplicationSync(row_number=row_number, created=created)
    finally:
        workbook.close()
        temp_path.unlink(missing_ok=True)
