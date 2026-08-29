"""Sync the local certification-tracker workbook into the Postgres tracker.

One worksheet per certification. Exact expected header row (row 1, columns
A-J): Name, Manager, Practice Director, Prep Start Date, End Date/Exam Date,
Cert Name, Status, Credly Link, Practice, Expiration Date.

Identity, not names, drives matching so renames don't create duplicates:
  - Each worksheet gets a hidden GUID written into cell Z1 the first time
    it's seen. certification_catalog.source_sheet_id matches on that GUID.
  - Each data row gets a hidden Record ID written into column L (header
    "_record_id", column hidden) the first time it's seen.
    certificate_records is unique on (source_sheet_id, source_row_id), so a
    resubmission (same sheet, same row) updates in place.

SEAM: `resolve_employee` below is the one place employee identity is looked
up for a sync row. Today it's a name match against the local `employees`
table (the HRIS stand-in). When Entra/Graph sync exists, this becomes a
lookup against synced directory data instead — nothing else in this module
needs to change.

Practice is never read from the sheet's "Practice" column as authoritative
(BRD requirement) — it's always taken from the matched employee's record.
An unmatched employee still gets a row (flagged needs_setup) rather than
being silently dropped, so it surfaces for manual review.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CertificateRecord, CertificationCatalog, Employee
from app.services.repository import certificate_record_repo
from app.services.verification import perform_verification

_EXPECTED_HEADERS = [
    "Name",
    "Manager",
    "Practice Director",
    "Prep Start Date",
    "End Date/Exam Date",
    "Cert Name",
    "Status",
    "Credly Link",
    "Practice",
    "Expiration Date",
]
_SHEET_ID_CELL = "Z1"
_RECORD_ID_COL = 12  # column L
_RECORD_ID_HEADER = "_record_id"

_STATUS_MAP = {
    "in progress": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "gave exam but failed": "gave_exam_but_failed",
    "failed": "gave_exam_but_failed",
}

ACTOR = "excel_sync"


@dataclass
class SyncReport:
    sheets_processed: int = 0
    catalog_entries_created: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_needing_setup: int = 0
    verifications_triggered: int = 0
    warnings: list[str] = field(default_factory=list)


def _get_or_create_sheet_id(ws: Worksheet) -> str:
    cell = ws[_SHEET_ID_CELL]
    if cell.value:
        return str(cell.value)
    new_id = str(uuid.uuid4())
    cell.value = new_id
    return new_id


def _get_or_create_row_id(ws: Worksheet, row_idx: int) -> str:
    header_cell = ws.cell(row=1, column=_RECORD_ID_COL)
    if not header_cell.value:
        header_cell.value = _RECORD_ID_HEADER
        ws.column_dimensions[header_cell.column_letter].hidden = True

    cell = ws.cell(row=row_idx, column=_RECORD_ID_COL)
    if cell.value:
        return str(cell.value)
    new_id = str(uuid.uuid4())
    cell.value = new_id
    return new_id


def _to_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _row_is_empty(values: dict) -> bool:
    return not any(str(v).strip() for v in values.values() if v is not None)


def resolve_employee(db: Session, name: str) -> Employee | None:
    """SEAM: name-match against the local employees table today; swap for a
    Graph/HRIS lookup once that sync exists."""
    if not name:
        return None
    return db.scalar(select(Employee).where(Employee.name.ilike(name.strip())))


def _get_or_create_catalog_entry(
    db: Session, sheet_id: str, sheet_title: str, cert_name_from_row: str | None, report: SyncReport
) -> CertificationCatalog:
    existing = db.scalar(
        select(CertificationCatalog).where(CertificationCatalog.source_sheet_id == sheet_id)
    )
    if existing:
        return existing

    name = (cert_name_from_row or sheet_title).strip()

    # A catalog entry with this exact name may already exist without a
    # source_sheet_id — e.g. seeded directly via /api/admin/catalog-entries
    # before this sheet was ever synced (scripts/seed_catalog_and_paths.py).
    # Adopt it (backfilling source_sheet_id) instead of creating a duplicate
    # pending_setup row that would shadow the real, fully-populated entry.
    by_name = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == name))
    if by_name:
        by_name.source_sheet_id = sheet_id
        db.flush()
        return by_name

    entry = CertificationCatalog(
        name=name,
        status="pending_setup",
        source_sheet_id=sheet_id,
    )
    db.add(entry)
    db.flush()
    report.catalog_entries_created += 1
    report.warnings.append(
        f"New certification catalog entry '{name}' created from sheet '{sheet_title}' "
        f"with status=pending_setup — needs provider/level/prerequisites/validity filled in by L&D."
    )
    return entry


def sync_tracker_workbook(db: Session, path: str) -> SyncReport:
    report = SyncReport()
    wb = openpyxl.load_workbook(path, data_only=True)

    for ws in wb.worksheets:
        header_row = [ws.cell(row=1, column=c).value for c in range(1, len(_EXPECTED_HEADERS) + 1)]
        if not any(header_row):
            continue  # blank sheet
        if [str(h).strip() if h else None for h in header_row] != _EXPECTED_HEADERS:
            report.warnings.append(
                f"Sheet '{ws.title}' header row doesn't match the expected columns — skipped."
            )
            continue

        report.sheets_processed += 1
        sheet_id = _get_or_create_sheet_id(ws)

        for row_idx in range(2, ws.max_row + 1):
            values = {
                header: ws.cell(row=row_idx, column=i + 1).value
                for i, header in enumerate(_EXPECTED_HEADERS)
            }
            if _row_is_empty(values):
                continue

            row_id = _get_or_create_row_id(ws, row_idx)
            needs_setup = False

            catalog_entry = _get_or_create_catalog_entry(
                db, sheet_id, ws.title, values["Cert Name"], report
            )

            employee_name = str(values["Name"] or "").strip()
            employee = resolve_employee(db, employee_name)
            if employee is None:
                needs_setup = True
                report.warnings.append(
                    f"Sheet '{ws.title}' row {row_idx}: no employee match for '{employee_name}' — "
                    f"flagged needs_setup, practice left unset."
                )

            status_raw = str(values["Status"] or "").strip().lower()
            submission_status = _STATUS_MAP.get(status_raw)
            if submission_status is None:
                needs_setup = True
                submission_status = "in_progress"
                report.warnings.append(
                    f"Sheet '{ws.title}' row {row_idx}: unrecognized Status '{values['Status']}' — "
                    f"defaulted to in_progress and flagged needs_setup."
                )

            credly_link = str(values["Credly Link"]).strip() if values["Credly Link"] else None
            verification_status = "not_applicable"
            if submission_status == "completed" and credly_link:
                verification_status = "pending"

            fields = dict(
                employee_id=employee.id if employee else None,
                employee_name=employee_name or (employee.name if employee else "Unknown"),
                manager_name=str(values["Manager"] or "").strip() or None,
                practice_director_name=str(values["Practice Director"] or "").strip() or None,
                practice=employee.practice if employee else None,
                certification_id=catalog_entry.id,
                cert_name=str(values["Cert Name"] or catalog_entry.name).strip(),
                submission_status=submission_status,
                verification_status=verification_status,
                prep_start_date=_to_date(values["Prep Start Date"]),
                exam_end_date=_to_date(values["End Date/Exam Date"]),
                credly_link=credly_link,
                expiration_date=_to_date(values["Expiration Date"]),
                needs_setup=needs_setup,
            )

            existing = db.scalar(
                select(CertificateRecord).where(
                    CertificateRecord.source_sheet_id == sheet_id,
                    CertificateRecord.source_row_id == row_id,
                )
            )

            if existing is None:
                record = certificate_record_repo.create(
                    db,
                    actor=ACTOR,
                    source_sheet_id=sheet_id,
                    source_row_id=row_id,
                    is_test_data=False,
                    **fields,
                )
                report.rows_created += 1
            else:
                before = {k: getattr(existing, k) for k in fields}
                certificate_record_repo.update_fields(db, existing, actor=ACTOR, changes=fields)
                record = existing
                if before != fields:
                    report.rows_updated += 1
                else:
                    report.rows_unchanged += 1

            if needs_setup:
                report.rows_needing_setup += 1

            if (
                record.submission_status == "completed"
                and record.credly_link
                and record.verification_status in ("pending", "not_applicable")
            ):
                try:
                    perform_verification(db, record, actor=ACTOR)
                    report.verifications_triggered += 1
                except Exception as exc:  # noqa: BLE001 - keep syncing other rows
                    report.warnings.append(
                        f"Sheet '{ws.title}' row {row_idx}: verification attempt failed: {exc}"
                    )

    db.commit()
    wb.save(path)
    return report
