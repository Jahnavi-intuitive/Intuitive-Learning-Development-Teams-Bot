from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditLog, CertificateRecord, Employee, VerificationResult
from app.schemas import CertificateRecordOut, TrackerQueryOut, VerificationResultOut
from app.services.access_scope import get_current_employee, require_visible, resolve_scope

router = APIRouter(prefix="/api/tracker", tags=["tracker"])


@router.get("/records", response_model=TrackerQueryOut)
def query_records(
    employee_id: str | None = None,
    submission_status: str | None = None,
    verification_status: str | None = None,
    cert_name: str | None = None,
    current: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    """Scoped tracker query — the core dashboard/agent read path.

    Access scope is resolved server-side from `current` (Design Principle
    5); `employee_id`, if passed, narrows within that scope and is rejected
    (403) if it names someone the caller isn't allowed to see.
    """
    scope = resolve_scope(db, current)

    if employee_id:
        require_visible(scope, employee_id)
        target_ids = {employee_id}
    else:
        target_ids = scope.visible_employee_ids

    stmt = (
        select(CertificateRecord)
        .join(Employee, CertificateRecord.employee_id == Employee.id, isouter=True)
        .where(Employee.employee_id.in_(target_ids))
    )
    if submission_status:
        stmt = stmt.where(CertificateRecord.submission_status == submission_status)
    if verification_status:
        stmt = stmt.where(CertificateRecord.verification_status == verification_status)
    if cert_name:
        stmt = stmt.where(CertificateRecord.cert_name.ilike(f"%{cert_name}%"))
    stmt = stmt.order_by(CertificateRecord.updated_at.desc())

    records = db.scalars(stmt).all()
    return TrackerQueryOut(
        records=[CertificateRecordOut.model_validate(r) for r in records],
        data_as_of=datetime.now(timezone.utc),
    )


@router.get("/records/{record_id}")
def get_record_detail(
    record_id: str,
    current: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    """Audit-ready history for a single record (BR-D-11): the record itself,
    every verification attempt, and every audit_log entry."""
    record = db.get(CertificateRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    scope = resolve_scope(db, current)
    owner_employee_id = None
    if record.employee_id:
        owner = db.get(Employee, record.employee_id)
        owner_employee_id = owner.employee_id if owner else None
    if owner_employee_id:
        require_visible(scope, owner_employee_id)

    verifications = db.scalars(
        select(VerificationResult)
        .where(VerificationResult.record_id == record.id)
        .order_by(VerificationResult.timestamp.desc())
    ).all()
    audit_entries = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "certificate_record", AuditLog.entity_id == record.id)
        .order_by(AuditLog.timestamp.desc())
    ).all()

    return {
        "record": CertificateRecordOut.model_validate(record),
        "verification_history": [VerificationResultOut.model_validate(v) for v in verifications],
        "audit_history": [
            {
                "action": a.action,
                "actor": a.actor,
                "old_value": a.old_value,
                "new_value": a.new_value,
                "timestamp": a.timestamp,
            }
            for a in audit_entries
        ],
        "data_as_of": datetime.now(timezone.utc),
    }


@router.get("/at-risk", response_model=TrackerQueryOut)
def at_risk(
    window_days: int = 30,
    current: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    """Certifications expiring within `window_days`, scoped to the caller
    (BR-D-13 / 'which of my reportees have a certification expiring soon')."""
    scope = resolve_scope(db, current)
    today = date.today()
    horizon = today + timedelta(days=window_days)

    stmt = (
        select(CertificateRecord)
        .join(Employee, CertificateRecord.employee_id == Employee.id, isouter=True)
        .where(
            Employee.employee_id.in_(scope.visible_employee_ids),
            CertificateRecord.expiration_date.is_not(None),
            CertificateRecord.expiration_date >= today,
            CertificateRecord.expiration_date <= horizon,
        )
        .order_by(CertificateRecord.expiration_date.asc())
    )
    records = db.scalars(stmt).all()
    return TrackerQueryOut(
        records=[CertificateRecordOut.model_validate(r) for r in records],
        data_as_of=datetime.now(timezone.utc),
    )
