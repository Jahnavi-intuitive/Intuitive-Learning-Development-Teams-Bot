"""Single reusable audit-logging function.

Every write to certificate_records (and any other audited entity) must call
`write_audit_entry` exactly once per change, through the repository layer —
never inline at individual router call sites — so the audit trail can't
silently drift out of sync with what actually gets written.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Session

from app.models import AuditLog


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "tolist"):  # numpy ndarray, as pgvector.Vector columns return
        return value.tolist()
    return value


def serialize_row(obj) -> dict:
    """Generic column-dict serializer for any mapped model instance — used
    for audit entries on certification_catalog / certification_prerequisites
    and every other admin-editable table (services/audited_crud.py), which
    (unlike certificate_records) don't have their own repository class.
    Kept separate from services/repository.py's CertificateRecord-specific
    version rather than sharing one, same reasoning as the duplicated
    curl_transport.py: small, stable, independent helpers.

    Vector columns (knowledge_base.embedding, escalations.question_embedding)
    are deliberately excluded, not just made JSON-safe: a multi-hundred-
    dimension float array in every audit_log row for every content edit is
    noise no one will ever read there — content_hash (see
    services/rag_content.py) is what tells you whether the embedded text
    actually changed."""
    return {
        col.name: _json_safe(getattr(obj, col.name))
        for col in obj.__table__.columns
        if not isinstance(col.type, Vector)
    }


def write_audit_entry(
    db: Session,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    actor: str,
    old_value: dict | None,
    new_value: dict | None,
) -> AuditLog:
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        old_value=old_value,
        new_value=new_value,
    )
    db.add(entry)
    return entry
