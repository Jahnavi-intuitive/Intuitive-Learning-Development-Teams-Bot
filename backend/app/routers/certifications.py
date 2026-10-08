from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificateRecord, CertificationCatalog, DriveCertification, DriveEnrollment, Employee
from app.portal_db import get_portal_db
from app.portal_models import CertificationRecord as PortalCertificationRecord
from app.portal_models import PartnerCertification
from app.schemas import CertificateRecordOut, SubmissionIn, SubmissionOut, VerificationResultOut
from app.services import catalog_provisioning, cert_progress, excel_sync
from app.services.access_scope import get_current_employee
from app.services.audited_crud import update_with_audit
from app.services.recommender.taxonomy import cert_key
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
    if catalog_entry is None:
        # Before 2026-09-13, an unmatched submission just left
        # certificate_records.certification_id NULL forever — a real gap,
        # since services/excel_sync.py already auto-created a catalog
        # entry for the equivalent case (an unrecognized name found in a
        # synced spreadsheet row). Both paths now go through the same
        # shared provisioning (short_name assigned, admins notified).
        catalog_entry, _ = catalog_provisioning.get_or_create_by_name(db, payload.cert_name)
    needs_setup = catalog_entry.status == "pending_setup"

    verification_status = "not_applicable"
    if payload.submission_status == "completed" and payload.credly_link:
        verification_status = "pending"

    record = certificate_record_repo.create(
        db,
        actor=payload.employee_id,
        employee_id=employee.id,
        employee_name=employee.name,
        employee_email=employee.email,
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
        excel_sync.write_back_to_sheet(db, record, catalog_entry)

    db.commit()
    db.refresh(record)

    return SubmissionOut(
        record=CertificateRecordOut.model_validate(record),
        verification=verification_out,
        data_as_of=datetime.now(timezone.utc),
    )


# --- Prep tracking: start a certification from chat, and see progress ------
class StartIn(BaseModel):
    employee_id: str
    cert_name: str


def _canonical_partner_name(portal_db: Session, name: str) -> tuple[str, str | None]:
    """Match a name the employee typed/said ("aws saa", "solutions architect
    associate") to the partner catalog's exact certification name, so the
    tracker and the catalog agree. Exact (normalized) match first, then the
    single best containment match; the input is kept as-is if nothing fits."""
    want = cert_key(None, name)[1]
    rows = portal_db.scalars(select(PartnerCertification).where(PartnerCertification.is_active.is_(True))).all()
    for r in rows:
        if cert_key(None, r.certification_name)[1] == want:
            return r.certification_name, r.level
    contains = [r for r in rows if want in cert_key(None, r.certification_name)[1]]
    if len(contains) == 1:
        return contains[0].certification_name, contains[0].level
    return name.strip(), None


def _progress_out(p: cert_progress.Progress) -> dict:
    r = p.record
    return {
        "cert_name": r.cert_name,
        "submission_status": r.submission_status,
        "verification_status": r.verification_status,
        "level": p.level,
        "prep_start_date": r.prep_start_date,
        "exam_end_date": r.exam_end_date,
        "expiration_date": r.expiration_date,
        "business_days_elapsed": p.business_days_elapsed,
        "target_business_days": p.target_business_days,
        "target_date": p.target_date,
        "overdue_business_days": p.overdue_business_days,
        "is_overdue": p.is_overdue,
    }


_PORTAL_STATUS = {"completed": "completed", "in_progress": "in_progress", "not_cleared": "gave_exam_but_failed"}


def _portal_rows(portal_db: Session, employee: Employee, tracked_keys: set) -> list[dict]:
    """The same person's rows in the partner-portal trackers (the synced
    SharePoint workbooks), matched by email then by name — the identity
    rule the recommender and the portal's View My Status already use.
    Certifications already in this system's own tracker are skipped so
    nothing shows twice."""
    email = (employee.email or "").strip().lower()
    name = (employee.name or "").strip().lower()
    rows = portal_db.scalars(select(PortalCertificationRecord)).all()
    out, seen = [], set()
    for r in rows:
        if not r.certification_name:
            continue
        if not ((email and r.person_email_normalized == email) or (r.person_name or "").strip().lower() == name):
            continue
        key = cert_key(None, r.certification_name)[1]
        if key in tracked_keys or key in seen:
            continue
        seen.add(key)
        status = _PORTAL_STATUS.get(getattr(r.status_canonical, "value", r.status_canonical), "in_progress")
        out.append({
            "cert_name": r.certification_name, "submission_status": status, "verification_status": None,
            "level": r.certification_level, "prep_start_date": r.prep_start_date, "exam_end_date": r.exam_date,
            "expiration_date": r.expiry_date, "completion_date": r.completion_date,
            "business_days_elapsed": None, "target_business_days": None, "target_date": None,
            "overdue_business_days": 0, "is_overdue": False, "source": "partner_portal",
        })
    return out


@router.get("/my-progress")
def my_progress(
    employee_id: str,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    current: Employee = Depends(get_current_employee),
):
    """Everything on file for the caller, one row per certification: this
    system's tracker (latest record per certification, with business days
    of prep so far vs. the level's target for anything in progress) plus
    the partner-portal trackers' rows for them (source="partner_portal").
    Self-only."""
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only view your own progress.")
    today = datetime.now(timezone.utc).date()
    target_map = cert_progress.targets(db)
    rows = [{**_progress_out(cert_progress.progress_for(db, r, today, target_map)), "source": "tracker"}
            for r in cert_progress.latest_records(db, current)]
    rows += _portal_rows(portal_db, current, {cert_key(None, r["cert_name"])[1] for r in rows})
    return {
        "employee_name": current.name,
        "first_name": (current.name or "").split(" ")[0] or None,
        "certifications": rows,
        "counts": {s: sum(1 for r in rows if r["submission_status"] == s) for s in sorted(VALID_STATUSES)},
        "prep_target_business_days": target_map,
        "data_as_of": datetime.now(timezone.utc),
    }


@router.post("/start")
def start_certification(
    payload: StartIn,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    current: Employee = Depends(get_current_employee),
):
    """Mark a certification as in progress for the caller, starting their
    prep clock today — the conversational shortcut for "yes, I'm starting
    this one", no form. Goes through the normal submission path (audit,
    drive linking, sheet write-back). If the certification is already in
    progress or completed for them, nothing new is created and the existing
    record comes back with already_tracked=True."""
    if payload.employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only start certifications for yourself.")
    name, partner_level = _canonical_partner_name(portal_db, payload.cert_name)
    today = datetime.now(timezone.utc).date()

    for r in cert_progress.latest_records(db, current):
        if cert_key(None, r.cert_name)[1] == cert_key(None, name)[1] and r.submission_status in ("in_progress", "completed"):
            p = cert_progress.progress_for(db, r, today)
            return {"already_tracked": True, "certification": _progress_out(p), "data_as_of": datetime.now(timezone.utc)}
    for row in _portal_rows(portal_db, current, set()):
        if cert_key(None, row["cert_name"])[1] == cert_key(None, name)[1] and row["submission_status"] in ("in_progress", "completed"):
            return {"already_tracked": True, "certification": row, "data_as_of": datetime.now(timezone.utc)}

    last = db.scalar(
        select(CertificateRecord).where(CertificateRecord.employee_id == current.id).order_by(CertificateRecord.created_at.desc())
    )
    submit_certification(
        SubmissionIn(
            employee_id=current.employee_id,
            cert_name=name,
            submission_status="in_progress",
            manager_name=(last.manager_name if last else None) or current.manager_name,
            practice_director_name=(last.practice_director_name if last else None) or current.practice_director_name,
            prep_start_date=today,
        ),
        db=db,
        current=current,
    )
    record = cert_progress.latest_records(db, current)[0]
    # A catalog entry auto-provisioned by that submission has no level yet;
    # take the partner catalog's, so the prep target is the right one.
    if partner_level and record.certification_id:
        entry = db.get(CertificationCatalog, record.certification_id)
        if entry and not entry.level:
            update_with_audit(db, entry, actor="system", changes={"level": partner_level})
            db.commit()
    return {"already_tracked": False, "certification": _progress_out(cert_progress.progress_for(db, record, today)),
            "data_as_of": datetime.now(timezone.utc)}
