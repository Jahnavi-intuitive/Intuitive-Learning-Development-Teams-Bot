"""The one write-path for certificate_records.

Every router/service that creates or mutates a CertificateRecord goes through
this repository so that audit logging (services.audit.write_audit_entry) is
never duplicated per call site, and so "what actually changed" is computed
consistently.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import CertificateRecord
from app.services.audit import write_audit_entry

_STATUS_FIELDS = {"submission_status", "verification_status"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def serialize_record(record: CertificateRecord) -> dict:
    return {
        col.name: _json_safe(getattr(record, col.name))
        for col in CertificateRecord.__table__.columns
    }


class CertificateRecordRepository:
    def create(self, db: Session, *, actor: str, **fields) -> CertificateRecord:
        record = CertificateRecord(**fields, created_by=actor, updated_by=actor)
        db.add(record)
        db.flush()  # assign PK before audit-logging it
        write_audit_entry(
            db,
            entity_type="certificate_record",
            entity_id=record.id,
            action="created",
            actor=actor,
            old_value=None,
            new_value=serialize_record(record),
        )
        return record

    def update_fields(
        self,
        db: Session,
        record: CertificateRecord,
        *,
        actor: str,
        changes: dict,
        action_override: str | None = None,
    ) -> CertificateRecord:
        old = serialize_record(record)
        status_changed = False
        actually_changed = False
        for key, value in changes.items():
            if getattr(record, key) != value:
                setattr(record, key, value)
                actually_changed = True
                if key in _STATUS_FIELDS:
                    status_changed = True

        if not actually_changed:
            return record

        record.updated_by = actor
        db.flush()
        new = serialize_record(record)

        action = action_override or ("status_changed" if status_changed else "field_updated")
        write_audit_entry(
            db,
            entity_type="certificate_record",
            entity_id=record.id,
            action=action,
            actor=actor,
            old_value=old,
            new_value=new,
        )
        return record


certificate_record_repo = CertificateRecordRepository()
