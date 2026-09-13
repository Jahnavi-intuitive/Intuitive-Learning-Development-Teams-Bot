"""PD/Leader x Status pivot computation — shared by routers/dashboards.py
(the live dashboard read) and services/excel_sync.py's SharePoint
write-back (the same pivot persisted into each provider's real workbook).
One computation, two consumers, per the user's own reference screenshot
of a real "Count of Status" pivot their org already uses (PD/Leader rows,
status columns, a Grand Total row/column).

practice_director_name is the row dimension — not the employee's manager —
since that rolls up to the practice's actual director regardless of how
many management layers sit between the director and the employee, which
is what a leadership-facing pivot is meant to show.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CertificateRecord, CertificationCatalog

# Matches the exact labels AND column order in the user's reference
# screenshot (Attempted - Not Cleared, Certified, In Progress).
STATUS_DISPLAY = {
    "gave_exam_but_failed": "Attempted - Not Cleared",
    "completed": "Certified",
    "in_progress": "In Progress",
}


def compute_pd_pivot_for_certification(
    db: Session, catalog_entry: CertificationCatalog
) -> dict[str, dict[str, int]]:
    """Returns {pd_name_or_"Unknown": {status_display: count, ...}, ...}
    for one certification. Empty dict if no certificate_records reference
    this certification at all yet."""
    rows = db.execute(
        select(
            CertificateRecord.practice_director_name,
            CertificateRecord.employee_name,
            CertificateRecord.submission_status,
        ).where(CertificateRecord.certification_id == catalog_entry.id)
    ).all()

    pivot: dict[str, dict[str, int]] = {}
    for pd_name, employee_name, submission_status in rows:
        # A practice director's own record has no PD above them (by
        # design — see the employee-seeding docstring) — attribute it to
        # themselves rather than an uninformative "Unknown" bucket, since
        # for a leadership-facing pivot "no PD recorded" for a PD's own
        # cert means "this person IS the PD," not "nobody knows."
        pd_label = pd_name or employee_name or "Unknown"
        status_label = STATUS_DISPLAY.get(submission_status, submission_status)
        row = pivot.setdefault(pd_label, {})
        row[status_label] = row.get(status_label, 0) + 1
    return pivot
