"""Shared drive-compliance computation — the single definition of
"compliant"/"compliance %" used by both routers/drives.py's per-drive
detail view (get_drive_detail) and routers/dashboards.py's org-wide
aggregate view (drive-compliance). Extracted 2026-09-14 specifically so
the leadership dashboard's numbers can never quietly drift from the
existing per-drive view's own numbers — one formula, two callers.

Compliance % itself is never stored (see CertificationDrive's own
docstring) — always computed fresh from drive_enrollments joined to
certificate_records.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import CertificationDrive, DriveEnrollment, Notification


def is_enrollment_compliant(enrollment: DriveEnrollment) -> bool:
    return (
        enrollment.certificate_record is not None
        and enrollment.certificate_record.submission_status == "completed"
    )


def compute_enrollment_counts(enrollments: list[DriveEnrollment]) -> dict:
    """Pure counting logic over already-fetched enrollments (each needs
    .certificate_record loaded, e.g. via selectinload) — no query of its
    own, so callers control how the enrollments were fetched (with or
    without the employee relationship loaded, depending on what else they
    need alongside the counts)."""
    enrolled_count = len(enrollments)
    exempted_count = sum(1 for e in enrollments if e.enrollment_status == "exempted")
    compliant_count = sum(1 for e in enrollments if is_enrollment_compliant(e))
    denominator = enrolled_count - exempted_count
    compliance_pct = round((compliant_count / denominator * 100) if denominator > 0 else 0.0, 1)
    return {
        "enrolled_count": enrolled_count,
        "compliant_count": compliant_count,
        "exempted_count": exempted_count,
        "compliance_pct": compliance_pct,
    }


def compute_drive_compliance_summary(db: Session, drive: CertificationDrive) -> dict:
    """The full aggregate row routers/dashboards.py needs for one drive:
    the shared counts above, plus how many distinct people currently sit
    at each escalation_level for this drive. Counts distinct recipients,
    not raw notification rows — a retried/re-sent reminder to the same
    person must not inflate "how many people are at this level.\""""
    enrollments = db.scalars(
        select(DriveEnrollment)
        .where(DriveEnrollment.drive_id == drive.id)
        .options(selectinload(DriveEnrollment.certificate_record))
    ).all()
    counts = compute_enrollment_counts(enrollments)

    escalation_rows = db.execute(
        select(Notification.escalation_level, Notification.recipient_employee_id).where(
            Notification.related_drive_id == drive.id,
            Notification.notification_type == "drive_escalation",
        )
    ).all()
    recipients_by_level: dict[str, set] = {}
    for level, recipient_id in escalation_rows:
        if level:
            recipients_by_level.setdefault(level, set()).add(recipient_id)
    escalation_counts = {level: len(ids) for level, ids in recipients_by_level.items()}

    return {
        "drive_id": str(drive.id),
        "name": drive.name,
        "target_scope": drive.target_scope,
        "target_scope_value": drive.target_scope_value,
        "deadline": drive.deadline.isoformat(),
        "status": drive.status,
        "escalation_counts": escalation_counts,
        **counts,
    }
