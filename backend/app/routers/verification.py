from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificateRecord
from app.schemas import VerificationResultOut
from app.services.verification import perform_verification

router = APIRouter(prefix="/api/verification", tags=["verification"])


@router.post("/{record_id}/retry", response_model=VerificationResultOut)
def retry_verification(record_id: str, db: Session = Depends(get_db)):
    """Manual re-run for a record sitting in the exception queue (Section
    5.2 exception-queue review). Never called by the agent directly —
    verification decisions come from this endpoint or the submission flow,
    not from agent reasoning."""
    record = db.get(CertificateRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    if not record.credly_link:
        raise HTTPException(status_code=422, detail="Record has no credly_link to verify against.")

    result = perform_verification(db, record, actor="admin_retry")
    db.commit()
    return VerificationResultOut.model_validate(result)
