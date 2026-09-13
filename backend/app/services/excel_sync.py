"""Sync certification-tracker workbooks into the Postgres tracker.

One file per certification provider, one worksheet per certification
within it (2026-09-13 — previously one shared file for everything; see
DEVELOPMENT_LOG.md for the migration). Files live in the SharePoint
"Certification Trackers" library (services/sharepoint_client.py) in
production; sync_tracker_workbook (a single local file) is kept as a
lighter-weight local-dev/test path, not removed.

Exact expected header row (row 1, columns A-N), same for every sheet in
every file (2026-09-13 rework — see DEVELOPMENT_LOG.md): Index, Name,
Email ID, Status, Manager, PD Leader, Practice, Cert, Prep Start Date,
Cert Done Date, Manager Mail, PD Mail, Credly Link, Expiration Date.
"Index" is a plain human-readable row number an admin may fill in for
their own reference — never read or written by this module, it's not a
business field.

Identity, not names, drives matching so renames don't create duplicates:
  - Each worksheet gets a hidden GUID written into cell Z1 the first time
    it's seen. certification_catalog.source_sheet_id matches on that GUID.
  - Each data row gets a hidden Record ID written into column O (header
    "_record_id", column hidden) the first time it's seen — moved from
    column L in the pre-2026-09-13 layout, since the visible header row
    grew from 10 to 14 columns and would otherwise collide with it.
    certificate_records is unique on (source_sheet_id, source_row_id), so a
    resubmission (same sheet, same row) updates in place.
  - A worksheet's *name* is a certification's short_name (services/
    short_name.py) — capped at 31 characters, Excel's own sheet-name limit
    — never the certification's real, full name.

SEAM: `resolve_employee` below is the one place employee identity is looked
up for a sync row. Tries an exact email match first (Email ID column) —
unambiguous, unlike a name — falling back to a name match only if no
email was given or it didn't match anything. Both are against the local
`employees` table (the HRIS stand-in) today; when Entra/Graph sync
exists, this becomes a lookup against synced directory data instead —
nothing else in this module needs to change.

Practice is never read from the sheet's "Practice" column as authoritative
(BRD requirement) — it's always taken from the matched employee's record.
An unmatched employee still gets a row (flagged needs_setup) rather than
being silently dropped, so it surfaces for manual review.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CertificateRecord, CertificationCatalog, Employee
from app.services import catalog_provisioning, sharepoint_client
from app.services.pd_pivot import STATUS_DISPLAY
from app.services.repository import certificate_record_repo
from app.services.verification import perform_verification

_PIVOT_SHEET_NAME = "Summary"
_PIVOT_STATUS_COLUMNS = list(STATUS_DISPLAY.values())  # fixed order, matches the reference screenshot

_EXPECTED_HEADERS = [
    "Index",
    "Name",
    "Email ID",
    "Status",
    "Manager",
    "PD Leader",
    "Practice",
    "Cert",
    "Prep Start Date",
    "Cert Done Date",
    "Manager Mail",
    "PD Mail",
    "Credly Link",
    "Expiration Date",
]
_SHEET_ID_CELL = "Z1"
_RECORD_ID_COL = 15  # column O — moved from L (12) when the visible header row grew to 14 columns
_RECORD_ID_HEADER = "_record_id"

_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill(fill_type="solid", start_color="2F5496", end_color="2F5496")
_HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _style_header_row(ws: Worksheet) -> None:
    """Purely cosmetic — bold white text on a dark blue fill, centered, so
    the header row actually reads as a header rather than a plain text
    row. Never touched by header *validation* (_sync_workbook compares
    cell values, not styles) or by any business logic."""
    for col_idx in range(1, len(_EXPECTED_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGNMENT
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"

_STATUS_MAP = {
    "in progress": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "gave exam but failed": "gave_exam_but_failed",
    "failed": "gave_exam_but_failed",
}
# The reverse of _STATUS_MAP, for write_back_to_sheet — one clean display
# string per enum value (not every input variation _STATUS_MAP accepts).
_STATUS_DISPLAY = {
    "in_progress": "In Progress",
    "completed": "Completed",
    "gave_exam_but_failed": "Gave exam but failed",
}

ACTOR = "excel_sync"

logger = logging.getLogger(__name__)


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
    # "Index" is a plain human-reference number, not business data — an
    # admin filling it in on an otherwise-untouched row shouldn't make
    # this look like a real submission worth syncing.
    return not any(str(v).strip() for k, v in values.items() if k != "Index" and v is not None)


def resolve_employee(db: Session, name: str, email: str | None = None) -> Employee | None:
    """SEAM: email/name-match against the local employees table today;
    swap for a Graph/HRIS lookup once that sync exists. Email is tried
    first when given — an exact match, and unambiguous unlike a name —
    falling back to a name match only if no email was given or nothing
    matched it."""
    if email:
        by_email = db.scalar(select(Employee).where(Employee.email.ilike(email.strip())))
        if by_email:
            return by_email
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

    # get_or_create_by_name already handles "a catalog entry with this exact
    # name may exist without a source_sheet_id" (e.g. seeded directly via
    # /api/admin/catalog-entries before this sheet was ever synced) by
    # returning that existing row rather than a duplicate — this function's
    # own job is just to also backfill source_sheet_id onto whatever comes
    # back, adopted or brand new, since that part is specific to Excel-sync
    # identity and the shared helper has no reason to know about it.
    entry, created = catalog_provisioning.get_or_create_by_name(db, name)
    if entry.source_sheet_id != sheet_id:
        entry.source_sheet_id = sheet_id
        db.flush()

    if created:
        report.catalog_entries_created += 1
        report.warnings.append(
            f"New certification catalog entry '{name}' (short name: {entry.short_name}) created from "
            f"sheet '{sheet_title}' with status=pending_setup — needs provider/level/prerequisites/"
            "validity filled in by L&D."
        )
    return entry


def _provider_filename(provider: str) -> str:
    """The .xlsx filename a provider's tracker workbook is stored under in
    the Certification Trackers library — e.g. "AWS" -> "AWS.xlsx". Strips
    characters invalid in a SharePoint/Windows filename; collapses to
    "Unknown Provider.xlsx" only in the pathological case of an
    all-invalid-characters provider name (should not happen in practice —
    provider is admin-entered, not free-form user input at scale)."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", provider).strip()
    return f"{cleaned or 'Unknown Provider'}.xlsx"


def _sync_workbook(db: Session, wb, workbook_label: str, report: SyncReport) -> None:
    """The actual per-worksheet sync loop, operating on an already-open
    openpyxl Workbook — shared by every caller (a single local file, or
    one of several files downloaded from SharePoint) so this logic exists
    exactly once regardless of where the bytes came from. Does not commit
    or save anything itself; callers own that."""
    for ws in wb.worksheets:
        header_row = [ws.cell(row=1, column=c).value for c in range(1, len(_EXPECTED_HEADERS) + 1)]
        if not any(header_row):
            continue  # blank sheet
        if [str(h).strip() if h else None for h in header_row] != _EXPECTED_HEADERS:
            report.warnings.append(
                f"[{workbook_label}] Sheet '{ws.title}' header row doesn't match the expected columns — skipped."
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
                db, sheet_id, ws.title, values["Cert"], report
            )

            employee_name = str(values["Name"] or "").strip()
            employee_email = str(values["Email ID"] or "").strip() or None
            employee = resolve_employee(db, employee_name, employee_email)
            if employee is None:
                needs_setup = True
                report.warnings.append(
                    f"[{workbook_label}] Sheet '{ws.title}' row {row_idx}: no employee match for "
                    f"'{employee_name}' — flagged needs_setup, practice left unset."
                )

            status_raw = str(values["Status"] or "").strip().lower()
            submission_status = _STATUS_MAP.get(status_raw)
            if submission_status is None:
                needs_setup = True
                submission_status = "in_progress"
                report.warnings.append(
                    f"[{workbook_label}] Sheet '{ws.title}' row {row_idx}: unrecognized Status "
                    f"'{values['Status']}' — defaulted to in_progress and flagged needs_setup."
                )

            credly_link = str(values["Credly Link"]).strip() if values["Credly Link"] else None
            verification_status = "not_applicable"
            if submission_status == "completed" and credly_link:
                verification_status = "pending"

            fields = dict(
                employee_id=employee.id if employee else None,
                employee_name=employee_name or (employee.name if employee else "Unknown"),
                employee_email=employee_email,
                manager_name=str(values["Manager"] or "").strip() or None,
                manager_email=str(values["Manager Mail"] or "").strip() or None,
                practice_director_name=str(values["PD Leader"] or "").strip() or None,
                practice_director_email=str(values["PD Mail"] or "").strip() or None,
                practice=employee.practice if employee else None,
                certification_id=catalog_entry.id,
                cert_name=str(values["Cert"] or catalog_entry.name).strip(),
                submission_status=submission_status,
                verification_status=verification_status,
                prep_start_date=_to_date(values["Prep Start Date"]),
                exam_end_date=_to_date(values["Cert Done Date"]),
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
                        f"[{workbook_label}] Sheet '{ws.title}' row {row_idx}: verification attempt "
                        f"failed: {exc}"
                    )


def sync_tracker_workbook(db: Session, path: str) -> SyncReport:
    """Local-file entry point — kept for local dev/testing without needing
    live SharePoint access on every run. Not used by the production sync
    path (see sync_all_provider_workbooks below)."""
    report = SyncReport()
    wb = openpyxl.load_workbook(path, data_only=True)
    _sync_workbook(db, wb, workbook_label=path, report=report)
    db.commit()
    wb.save(path)
    return report


def sync_all_provider_workbooks(db: Session) -> SyncReport:
    """The production entry point: every .xlsx file currently in the
    SharePoint Certification Trackers library is downloaded, synced, and
    re-uploaded — one shared report across all of them. Each file is
    committed and re-uploaded individually (not one giant transaction for
    every provider) so a problem with one provider's file doesn't block
    the others from syncing."""
    report = SyncReport()
    for filename in sharepoint_client.list_files():
        content = sharepoint_client.download_file(filename)
        if content is None:
            continue  # listed a moment ago, gone now — someone else's concurrent edit; skip, not fatal
        wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
        _sync_workbook(db, wb, workbook_label=filename, report=report)
        db.commit()

        buffer = BytesIO()
        wb.save(buffer)
        sharepoint_client.upload_file(filename, buffer.getvalue())
    return report


def ensure_certification_sheet_exists(db: Session, catalog_entry: CertificationCatalog) -> None:
    """Called once a certification's provider becomes known (see
    routers/catalog.py's admin update endpoint) — creates that provider's
    workbook if it doesn't exist yet, and/or a new sheet named
    catalog_entry.short_name within it if that sheet doesn't exist yet.
    A no-op if both already exist. Requires short_name and provider to
    both already be set; callers are expected to have checked that."""
    if not catalog_entry.provider or not catalog_entry.short_name:
        raise ValueError("ensure_certification_sheet_exists requires both provider and short_name to be set.")

    filename = _provider_filename(catalog_entry.provider)
    content = sharepoint_client.download_file(filename)
    wb = openpyxl.load_workbook(BytesIO(content), data_only=True) if content is not None else openpyxl.Workbook()

    if content is None:
        # A brand-new workbook — remove openpyxl's default blank "Sheet"
        # once the real one is added below, so the file doesn't carry a
        # meaningless leftover tab.
        default_sheet = wb.active
    else:
        default_sheet = None

    if catalog_entry.short_name in wb.sheetnames:
        return  # already has its sheet — nothing to do, don't touch existing data

    ws = wb.create_sheet(title=catalog_entry.short_name)
    for col_idx, header in enumerate(_EXPECTED_HEADERS, start=1):
        ws.cell(row=1, column=col_idx, value=header)
    _style_header_row(ws)
    sheet_id = _get_or_create_sheet_id(ws)  # writes a fresh GUID into Z1
    # Set directly, not left for the next sync to backfill by name-match —
    # we already know exactly which catalog entry this new sheet belongs
    # to, no reason to make a future sync re-derive it.
    catalog_entry.source_sheet_id = sheet_id
    db.flush()

    if default_sheet is not None:
        wb.remove(default_sheet)

    buffer = BytesIO()
    wb.save(buffer)
    sharepoint_client.upload_file(filename, buffer.getvalue())


def write_back_to_sheet(db: Session, record: CertificateRecord, catalog_entry: CertificationCatalog) -> None:
    """The reverse of _sync_workbook: writes one CertificateRecord's
    current values into its provider's SharePoint sheet, instead of only
    ever reading FROM Excel into Postgres. Called explicitly from
    routers/certifications.py's submission path — the one write path into
    certificate_records that doesn't itself originate from an Excel sync.
    Deliberately never hooked into services/repository.py's shared
    create/update methods: excel_sync.py's own calls into that repo (the
    read direction) would otherwise trigger a write straight back into the
    same row just read, fighting the in-progress workbook save at the end
    of sync_all_provider_workbooks.

    A no-op if the certification's provider isn't known yet — there's no
    file to write into, and this does not retroactively backfill rows
    once one gets created later (a real, accepted gap, not solved here).

    Best-effort by design, unlike the Graph-identity paths elsewhere in
    this codebase that fail closed: Postgres is this table's actual
    source of truth (see the model's own docstring), and Excel is a
    synced-out convenience copy for L&D, not the other way around. A
    transient SharePoint hiccup should never cost an employee their
    actual submission — it should just leave the Excel copy briefly
    stale, self-healing on the next successful write-back or the next
    full sync. Every failure is logged, never silently swallowed.

    Uses the same row-identity mechanism the read direction relies on:
    once record.source_sheet_id/source_row_id are set, a later call (e.g.
    a resubmission) finds and updates that exact row instead of appending
    a duplicate."""
    if not catalog_entry.provider or not catalog_entry.short_name:
        return

    try:
        filename = _provider_filename(catalog_entry.provider)
        content = sharepoint_client.download_file(filename)
        if content is None:
            # Shouldn't normally happen — ensure_certification_sheet_exists
            # creates the file the moment provider is set — but don't fail
            # a live submission over an inconsistency; create fresh.
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
        else:
            wb = openpyxl.load_workbook(BytesIO(content))

        if catalog_entry.short_name not in wb.sheetnames:
            ws = wb.create_sheet(title=catalog_entry.short_name)
            for col_idx, header in enumerate(_EXPECTED_HEADERS, start=1):
                ws.cell(row=1, column=col_idx, value=header)
            _style_header_row(ws)
        else:
            ws = wb[catalog_entry.short_name]
        sheet_id = _get_or_create_sheet_id(ws)

        row_idx = None
        if record.source_sheet_id == sheet_id and record.source_row_id:
            for r in range(2, ws.max_row + 1):
                if str(ws.cell(row=r, column=_RECORD_ID_COL).value or "") == record.source_row_id:
                    row_idx = r
                    break
        if row_idx is None:
            row_idx = ws.max_row + 1 if ws.max_row >= 1 else 2

        row_id_cell = ws.cell(row=row_idx, column=_RECORD_ID_COL)
        row_id_cell.value = record.source_row_id or str(uuid.uuid4())
        ws.column_dimensions[row_id_cell.column_letter].hidden = True

        values_by_header = {
            "Name": record.employee_name,
            "Email ID": record.employee_email,
            "Status": _STATUS_DISPLAY.get(record.submission_status, record.submission_status),
            "Manager": record.manager_name,
            "PD Leader": record.practice_director_name,
            "Practice": record.practice,
            "Cert": record.cert_name,
            "Prep Start Date": record.prep_start_date,
            "Cert Done Date": record.exam_end_date,
            "Manager Mail": record.manager_email,
            "PD Mail": record.practice_director_email,
            "Credly Link": record.credly_link,
            "Expiration Date": record.expiration_date,
        }
        for col_idx, header in enumerate(_EXPECTED_HEADERS, start=1):
            if header in values_by_header:
                ws.cell(row=row_idx, column=col_idx, value=values_by_header[header])

        record.source_sheet_id = sheet_id
        record.source_row_id = row_id_cell.value
        db.flush()

        buffer = BytesIO()
        wb.save(buffer)
        sharepoint_client.upload_file(filename, buffer.getvalue())
    except Exception:
        logger.warning(
            "Could not write certificate_record %s back to its SharePoint sheet — Postgres write "
            "still stands; will retry on the next write-back or full sync.",
            record.id,
            exc_info=True,
        )


def write_pd_pivot_summary_sheet(
    provider: str, blocks: list[tuple[CertificationCatalog, dict[str, dict[str, int]]]]
) -> None:
    """Writes (or replaces) a "Summary" sheet in one provider's SharePoint
    workbook containing that provider's PD/Leader x Status pivot for every
    one of its certifications with at least one certificate_record —
    stacked one block per certification, matching the shape of the user's
    own reference screenshot (PD/Leader rows, status columns, a Grand
    Total row and column). Explicit admin action
    (routers/dashboards.py::write_pd_pivot_to_sharepoint), not run on
    every dashboard read.

    Always regenerates the whole sheet from scratch rather than trying to
    diff/update it in place — this is a derived, read-only summary (never
    hand-edited by an admin the way a certification's own tracker sheet
    might be), so there's nothing to preserve across a rewrite."""
    filename = _provider_filename(provider)
    content = sharepoint_client.download_file(filename)
    if content is None:
        raise ValueError(f"No SharePoint workbook exists yet for provider {provider!r}.")
    wb = openpyxl.load_workbook(BytesIO(content))

    if _PIVOT_SHEET_NAME in wb.sheetnames:
        wb.remove(wb[_PIVOT_SHEET_NAME])
    ws = wb.create_sheet(title=_PIVOT_SHEET_NAME, index=0)  # first tab — the summary, not buried

    row_idx = 1
    for catalog_entry, pivot_rows in blocks:
        ws.cell(row=row_idx, column=1, value=f"Certification: {catalog_entry.name} ({catalog_entry.short_name})")
        ws.cell(row=row_idx, column=1).font = Font(bold=True)
        row_idx += 1

        header = ["PD/Leader", *_PIVOT_STATUS_COLUMNS, "Grand Total"]
        for col_idx, label in enumerate(header, start=1):
            ws.cell(row=row_idx, column=col_idx, value=label)
        _style_header_row_range(ws, row_idx, len(header))
        header_row_idx = row_idx
        row_idx += 1

        column_totals = [0] * len(_PIVOT_STATUS_COLUMNS)
        for pd_name in sorted(pivot_rows.keys()):
            status_counts = pivot_rows[pd_name]
            ws.cell(row=row_idx, column=1, value=pd_name)
            row_total = 0
            for col_idx, status_label in enumerate(_PIVOT_STATUS_COLUMNS, start=2):
                count = status_counts.get(status_label, 0)
                if count:
                    ws.cell(row=row_idx, column=col_idx, value=count)
                column_totals[col_idx - 2] += count
                row_total += count
            ws.cell(row=row_idx, column=len(header), value=row_total)
            row_idx += 1

        ws.cell(row=row_idx, column=1, value="Grand Total").font = Font(bold=True)
        for col_idx, total in enumerate(column_totals, start=2):
            cell = ws.cell(row=row_idx, column=col_idx, value=total)
            cell.font = Font(bold=True)
        ws.cell(row=row_idx, column=len(header), value=sum(column_totals)).font = Font(bold=True)
        row_idx += 2  # blank separator row before the next certification's block

    for col_idx in range(1, len(_PIVOT_STATUS_COLUMNS) + 3):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = 22

    buffer = BytesIO()
    wb.save(buffer)
    sharepoint_client.upload_file(filename, buffer.getvalue())


def _style_header_row_range(ws: Worksheet, row: int, num_cols: int) -> None:
    """Same visual treatment as _style_header_row, but for an arbitrary
    row/column range rather than always row 1 / the full _EXPECTED_HEADERS
    width — the pivot summary sheet has multiple header rows, one per
    certification block, not a single row-1 header."""
    for col_idx in range(1, num_cols + 1):
        cell = ws.cell(row=row, column=col_idx)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGNMENT
