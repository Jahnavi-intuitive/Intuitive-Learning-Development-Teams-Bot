"""Certification drives — mandatory campaigns (e.g. "everyone in Data+AI
gets an AWS cert by Q4"). Every mutation here goes through the same
audited_crud / admin_auth plumbing every other admin-editable table uses —
there is no drive-specific audit or auth logic.

Compliance % is never stored: get_drive_detail computes it fresh on every
read from drive_enrollments joined to certificate_records, so a query
never has to wonder whether a cached number is stale (Section 2 of the
2026-08-25 drives spec — see DEVELOPMENT_LOG.md).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import (
    CertificationCatalog,
    CertificationDrive,
    DriveCertification,
    DriveEnrollment,
    Employee,
)
from app.schemas import (
    DriveCreateIn,
    DriveDetailOut,
    DriveEnrollmentOut,
    DriveObligationOut,
    DriveOut,
    DriveUpdateIn,
    ExemptEnrollmentIn,
    MyDriveObligationsOut,
)
from app.services.access_scope import get_current_employee
from app.services.admin_auth import require_admin
from app.services.audited_crud import create_with_audit, delete_with_audit, update_with_audit

router = APIRouter(tags=["drives"])


def _resolve_certifications(db: Session, names: list[str]) -> list[CertificationCatalog]:
    certs = []
    for name in names:
        cert = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == name))
        if not cert:
            raise HTTPException(status_code=422, detail=f"Unknown certification '{name}'.")
        certs.append(cert)
    return certs


def _drive_or_404(db: Session, drive_id: str) -> CertificationDrive:
    drive = db.get(CertificationDrive, drive_id)
    if not drive:
        raise HTTPException(status_code=404, detail="Drive not found")
    return drive


@router.post("/api/admin/drives", response_model=DriveOut, status_code=201)
def create_drive(
    payload: DriveCreateIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    certs = _resolve_certifications(db, payload.certification_names)

    drive = create_with_audit(
        db,
        CertificationDrive,
        actor=admin.employee_id,
        name=payload.name,
        description=payload.description,
        target_scope=payload.target_scope,
        target_scope_value=payload.target_scope_value,
        target_metric_type=payload.target_metric_type,
        target_value=payload.target_value,
        start_date=payload.start_date,
        deadline=payload.deadline,
        reminder_cadence=payload.reminder_cadence.model_dump() if payload.reminder_cadence else None,
        status="draft",
        is_test_data=payload.is_test_data,
        created_by=admin.employee_id,
    )
    for cert in certs:
        create_with_audit(
            db, DriveCertification, actor=admin.employee_id, drive_id=drive.id, certification_id=cert.id
        )

    db.commit()
    db.refresh(drive)
    return drive


@router.get("/api/admin/drives", response_model=list[DriveOut])
def list_drives(status: str | None = None, db: Session = Depends(get_db), admin: Employee = Depends(require_admin)):
    stmt = select(CertificationDrive)
    if status:
        stmt = stmt.where(CertificationDrive.status == status)
    stmt = stmt.order_by(CertificationDrive.created_at.desc())
    return db.scalars(stmt).all()


@router.patch("/api/admin/drives/{drive_id}", response_model=DriveOut)
def update_drive(
    drive_id: str,
    payload: DriveUpdateIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    drive = _drive_or_404(db, drive_id)
    changes = payload.model_dump(exclude_unset=True)
    update_with_audit(db, drive, actor=admin.employee_id, changes=changes)
    db.commit()
    db.refresh(drive)
    return drive


def _compute_detail(db: Session, drive: CertificationDrive) -> DriveDetailOut:
    required_certs = db.scalars(
        select(CertificationCatalog)
        .join(DriveCertification, DriveCertification.certification_id == CertificationCatalog.id)
        .where(DriveCertification.drive_id == drive.id)
    ).all()

    enrollments = db.scalars(
        select(DriveEnrollment)
        .where(DriveEnrollment.drive_id == drive.id)
        .options(selectinload(DriveEnrollment.employee), selectinload(DriveEnrollment.certificate_record))
    ).all()

    enrolled_count = len(enrollments)
    exempted_count = sum(1 for e in enrollments if e.enrollment_status == "exempted")
    compliant_count = 0
    enrollment_rows = []
    for e in enrollments:
        # Live-computed, never trusting the cached enrollment_status column
        # for the compliance figure itself (Section 2 of the drives spec).
        is_compliant = (
            e.certificate_record is not None and e.certificate_record.submission_status == "completed"
        )
        if is_compliant:
            compliant_count += 1
        enrollment_rows.append(
            DriveEnrollmentOut(
                id=e.id,
                employee_id=e.employee.employee_id,
                employee_name=e.employee.name,
                enrollment_status=e.enrollment_status,
                is_compliant=is_compliant,
                certificate_record_id=e.certificate_record_id,
                exemption_reason=e.exemption_reason,
                enrolled_at=e.enrolled_at,
            )
        )

    denominator = enrolled_count - exempted_count
    compliance_pct = (compliant_count / denominator * 100) if denominator > 0 else 0.0

    return DriveDetailOut(
        drive=DriveOut.model_validate(drive),
        required_certifications=[c.name for c in required_certs],
        enrolled_count=enrolled_count,
        compliant_count=compliant_count,
        exempted_count=exempted_count,
        compliance_pct=round(compliance_pct, 1),
        enrollments=enrollment_rows,
        data_as_of=datetime.now(timezone.utc),
    )


@router.get("/api/admin/drives/{drive_id}", response_model=DriveDetailOut)
def get_drive_detail(drive_id: str, db: Session = Depends(get_db), admin: Employee = Depends(require_admin)):
    drive = _drive_or_404(db, drive_id)
    return _compute_detail(db, drive)


@router.post("/api/admin/drives/{drive_id}/enroll-population", response_model=DriveDetailOut)
def enroll_population(drive_id: str, db: Session = Depends(get_db), admin: Employee = Depends(require_admin)):
    """Query employees by the drive's target scope and populate
    drive_enrollments — a deliberate re-sync action, safe to call again
    later to pick up new joiners (existing enrollments are left untouched,
    never duplicated or reset)."""
    drive = _drive_or_404(db, drive_id)

    if drive.target_scope in ("practice", "department"):
        # The local employees table (services/access_scope.py's stand-in
        # for the HRIS) has no separate department field today — only
        # practice — and per the 2026-08-25 follow-up, that's fine:
        # 'department' is treated as an alias for 'practice', resolved the
        # same way. If a real department attribute is ever added, this is
        # the one place to split them apart again.
        target_employees = db.scalars(
            select(Employee).where(Employee.practice == drive.target_scope_value)
        ).all()
    elif drive.target_scope == "organization":
        target_employees = db.scalars(select(Employee)).all()
    else:
        raise HTTPException(status_code=422, detail=f"Unknown target_scope '{drive.target_scope}'.")

    already_enrolled_ids = set(
        db.scalars(select(DriveEnrollment.employee_id).where(DriveEnrollment.drive_id == drive.id)).all()
    )

    newly_enrolled = 0
    for employee in target_employees:
        if employee.id in already_enrolled_ids:
            continue
        create_with_audit(
            db,
            DriveEnrollment,
            actor=admin.employee_id,
            drive_id=drive.id,
            employee_id=employee.id,
            enrollment_status="not_started",
            is_test_data=employee.is_test_data,
        )
        newly_enrolled += 1

    if drive.status == "draft":
        update_with_audit(db, drive, actor=admin.employee_id, changes={"status": "active"})

    db.commit()
    db.refresh(drive)
    return _compute_detail(db, drive)


@router.post("/api/admin/drives/{drive_id}/enrollments/{employee_id}/exempt", response_model=DriveEnrollmentOut)
def exempt_enrollment(
    drive_id: str,
    employee_id: str,
    payload: ExemptEnrollmentIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    employee = db.scalar(select(Employee).where(Employee.employee_id == employee_id))
    if not employee:
        raise HTTPException(status_code=404, detail="Unknown employee_id")
    enrollment = db.scalar(
        select(DriveEnrollment).where(
            DriveEnrollment.drive_id == drive_id, DriveEnrollment.employee_id == employee.id
        )
    )
    if not enrollment:
        raise HTTPException(status_code=404, detail="This employee is not enrolled in this drive.")

    update_with_audit(
        db,
        enrollment,
        actor=admin.employee_id,
        changes={"enrollment_status": "exempted", "exemption_reason": payload.reason},
    )
    db.commit()
    db.refresh(enrollment)
    return DriveEnrollmentOut(
        id=enrollment.id,
        employee_id=employee.employee_id,
        employee_name=employee.name,
        enrollment_status=enrollment.enrollment_status,
        is_compliant=False,
        certificate_record_id=enrollment.certificate_record_id,
        exemption_reason=enrollment.exemption_reason,
        enrolled_at=enrollment.enrolled_at,
    )


@router.post("/api/admin/drives/{drive_id}/close", response_model=DriveOut)
def close_drive(drive_id: str, db: Session = Depends(get_db), admin: Employee = Depends(require_admin)):
    """Closing preserves the compliance record and audit trail — it never
    touches certification_catalog, learning_paths, or any other table.
    This is the UI's default action once a drive has any enrollments;
    delete_drive is only for a draft created by mistake."""
    drive = _drive_or_404(db, drive_id)
    update_with_audit(db, drive, actor=admin.employee_id, changes={"status": "closed"})
    db.commit()
    db.refresh(drive)
    return drive


@router.delete("/api/admin/drives/{drive_id}")
def delete_drive(
    drive_id: str,
    confirm: bool = False,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """Hard-delete — only permitted when the drive has zero enrollments
    (e.g. a draft created by mistake). A drive with any enrollment history
    must be closed instead: deleting it would erase the evidence of who
    did or didn't comply. Requires explicit confirm=true even then, so a
    stray DELETE call can't silently remove a real draft."""
    drive = _drive_or_404(db, drive_id)
    any_enrollment = db.scalar(
        select(DriveEnrollment).where(DriveEnrollment.drive_id == drive.id)
    )
    if any_enrollment is not None:
        raise HTTPException(
            status_code=409,
            detail="This drive has enrollment history — close it instead of deleting it.",
        )
    if not confirm:
        raise HTTPException(status_code=422, detail="Pass confirm=true to permanently delete this drive.")

    for dc in db.scalars(select(DriveCertification).where(DriveCertification.drive_id == drive.id)).all():
        db.delete(dc)
    delete_with_audit(db, drive, actor=admin.employee_id)
    db.commit()
    return {"deleted": True, "id": drive_id}


@router.get("/api/drives/my-obligations", response_model=MyDriveObligationsOut)
def my_drive_obligations(
    employee_id: str,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    """Employee-facing (not admin-gated) — backs the agent's
    get_my_drive_obligations tool. Structured metadata only (deadlines,
    status), never RAG-retrieved — see Section 5 of the drives spec.

    Self-only: identity is still checked (get_current_employee), same fix
    as learning_paths.py's /next and /overview — this had no auth
    dependency at all before the 2026-08-27 security pass."""
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only query your own drive obligations.")
    employee = current

    enrollments = db.scalars(
        select(DriveEnrollment)
        .where(DriveEnrollment.employee_id == employee.id)
        .options(selectinload(DriveEnrollment.drive), selectinload(DriveEnrollment.certificate_record))
    ).all()

    obligations = []
    for e in enrollments:
        if e.drive.status == "closed":
            continue
        required_certs = db.scalars(
            select(CertificationCatalog)
            .join(DriveCertification, DriveCertification.certification_id == CertificationCatalog.id)
            .where(DriveCertification.drive_id == e.drive.id)
        ).all()
        is_compliant = (
            e.certificate_record is not None and e.certificate_record.submission_status == "completed"
        )
        obligations.append(
            DriveObligationOut(
                drive_name=e.drive.name,
                required_certifications=[c.name for c in required_certs],
                deadline=e.drive.deadline,
                enrollment_status=e.enrollment_status,
                is_compliant=is_compliant,
            )
        )

    return MyDriveObligationsOut(obligations=obligations, data_as_of=datetime.now(timezone.utc))
