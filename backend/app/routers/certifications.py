from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificationCatalog, DriveCertification, DriveEnrollment, Employee
from app.schemas import CertificateRecordOut, SubmissionIn, SubmissionOut, VerificationResultOut
from app.services.access_scope import get_current_employee
from app.services.audited_crud import update_with_audit
from app.services.repository import certificate_record_repo
from app.services.verification import perform_verification

router = APIRouter(prefix="/api/certifications", tags=["certifications"])

VALID_STATUSES = {"in_progress", "completed", "gave_exam_but_failed"}


def _link_drive_enrollments(db: Session, employee: Employee, catalog_entry: CertificationCatalog, record) -> None:
    """When a submission matches a certification required by a drive the
    employee is enrolled in, link this record to that enrollment so
    routers/drives.py's live compliance % picks it up on its next read —
    drive_enrollments.certificate_record_id is never set any other way.
    Only touches not-yet-exempted enrollments for still-open (non-closed)
    drives; an exempted employee's enrollment_status is left as-is."""
    matching_drive_ids = db.scalars(
        select(DriveCertification.drive_id).where(DriveCertification.certification_id == catalog_entry.id)
    ).all()
    if not matching_drive_ids:
        return

    enrollments = db.scalars(
        select(DriveEnrollment).where(
            DriveEnrollment.employee_id == employee.id,
            DriveEnrollment.drive_id.in_(matching_drive_ids),
            DriveEnrollment.enrollment_status != "exempted",
        )
    ).all()
    for enrollment in enrollments:
        new_status = "completed" if record.submission_status == "completed" else "in_progress"
        update_with_audit(
            db,
            enrollment,
            actor=employee.employee_id,
            changes={"certificate_record_id": record.id, "enrollment_status": new_status},
        )


@router.post("/submit", response_model=SubmissionOut)
def submit_certification(
    payload: SubmissionIn,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    """Self-only: payload.employee_id must be the authenticated caller's
    own. This had no identity check at all before the 2026-08-27 security
    pass — payload.employee_id was trusted straight from the request body,
    meaning anyone could submit a certification record as anyone else."""
    if payload.employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only submit certifications for yourself.")

    if payload.submission_status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"submission_status must be one of {sorted(VALID_STATUSES)}")

    employee = current

    if payload.submission_status == "completed" and not payload.credly_link:
        raise HTTPException(
            status_code=422, detail="credly_link is required when submission_status is 'completed'."
        )

    catalog_entry = db.scalar(
        select(CertificationCatalog).where(CertificationCatalog.name.ilike(payload.cert_name.strip()))
    )
    needs_setup = catalog_entry is None

    verification_status = "not_applicable"
    if payload.submission_status == "completed" and payload.credly_link:
        verification_status = "pending"

    record = certificate_record_repo.create(
        db,
        actor=payload.employee_id,
        employee_id=employee.id,
        employee_name=employee.name,
        manager_name=payload.manager_name,
        practice_director_name=payload.practice_director_name,
        practice=employee.practice,
        certification_id=catalog_entry.id if catalog_entry else None,
        cert_name=payload.cert_name.strip(),
        submission_status=payload.submission_status,
        verification_status=verification_status,
        prep_start_date=payload.prep_start_date,
        exam_end_date=payload.exam_end_date,
        credly_link=payload.credly_link,
        expiration_date=payload.expiration_date,
        needs_setup=needs_setup,
        is_test_data=employee.is_test_data,
    )

    verification_out = None
    if verification_status == "pending":
        result = perform_verification(db, record, actor="system")
        verification_out = VerificationResultOut.model_validate(result)

    if catalog_entry:
        _link_drive_enrollments(db, employee, catalog_entry, record)

    db.commit()
    db.refresh(record)

    return SubmissionOut(
        record=CertificateRecordOut.model_validate(record),
        verification=verification_out,
        data_as_of=datetime.now(timezone.utc),
    )
