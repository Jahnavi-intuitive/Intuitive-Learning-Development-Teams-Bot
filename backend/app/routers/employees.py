from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificateRecord, Employee
from app.schemas import EmployeeOut
from app.services.access_scope import get_current_employee

router = APIRouter(prefix="/api/employees", tags=["employees"])


@router.get("", response_model=list[EmployeeOut])
def list_employees(db: Session = Depends(get_db)):
    """Dev-tool listing (e.g. to populate the test UI's 'acting as'
    dropdown) — not an agent tool; there's no legitimate agent question
    that needs "list everyone in the company"."""
    return db.scalars(select(Employee).order_by(Employee.name)).all()


@router.get("/me", response_model=EmployeeOut)
def get_me(current: Employee = Depends(get_current_employee)):
    return current


@router.get("/{employee_id}/prefill")
def prefill_for_new_submission(
    employee_id: str,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    """Manager/Practice Director prefill from the employee's most recent
    prior submission, per the submission-form requirement — falls back to
    the employee's directory record if they have no prior submissions.

    Self-only, same fix as the other employee-scoped tool endpoints (see
    the 2026-08-27 security-pass dev log entry) — this had no identity
    check at all before."""
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only prefill your own submission form.")
    employee = current

    last_record = db.scalar(
        select(CertificateRecord)
        .where(CertificateRecord.employee_id == employee.id)
        .order_by(CertificateRecord.created_at.desc())
    )

    return {
        "manager_name": (last_record.manager_name if last_record else None) or employee.manager_name,
        "practice_director_name": (
            last_record.practice_director_name if last_record else None
        )
        or employee.practice_director_name,
        "practice": employee.practice,
    }
