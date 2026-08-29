"""Single verification code path.

Both the submission API (routers/certifications.py) and the Excel tracker
sync (services/excel_sync.py) call `perform_verification` rather than each
implementing their own call-Credly-and-decide logic, per Design Principle 3
(certification status/verification logic computed in one place).
"""

from sqlalchemy.orm import Session

from app.models import CertificateRecord, VerificationResult
from app.services.credly import verify_against_credly
from app.services.repository import certificate_record_repo


def perform_verification(db: Session, record: CertificateRecord, *, actor: str = "system") -> VerificationResult:
    """Run Credly verification for a completed submission with a Credly link.

    Always writes a VerificationResult row and updates the record's
    verification_status via the repository (so it's audit-logged). Never
    sets verification_status="verified" except when both the recipient-name
    and cert-name fuzzy matches clear the confidence threshold.
    """
    if not record.credly_link:
        raise ValueError("perform_verification called without a credly_link")

    outcome = verify_against_credly(
        record.credly_link,
        expected_name=record.employee_name,
        expected_cert_name=record.cert_name,
    )

    result = VerificationResult(
        record_id=record.id,
        method="credly_link_check",
        extracted_recipient_name=outcome.extraction.recipient_name,
        extracted_badge_name=outcome.extraction.badge_name,
        extracted_issued_date=outcome.extraction.issued_date,
        extracted_expiration_date=outcome.extraction.expiration_date,
        name_match_score=outcome.name_match_score,
        cert_match=outcome.cert_match,
        status=outcome.status,
        verifier="system",
    )
    db.add(result)

    changes = {"verification_status": outcome.status}
    if outcome.status == "verified" and outcome.extraction.expiration_date:
        # Prefer the issuer's expiration date over whatever the form captured.
        changes["expiration_date"] = outcome.extraction.expiration_date

    certificate_record_repo.update_fields(
        db, record, actor=actor, changes=changes, action_override="verified"
    )
    return result
