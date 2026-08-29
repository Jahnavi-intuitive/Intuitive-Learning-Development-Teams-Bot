"""Generic, model-agnostic audit-logged create/update/delete.

services/repository.py's CertificateRecordRepository was written specific
to CertificateRecord (it needs its own status_changed-vs-field_updated
distinction). Every other admin-editable table added since then
(certification_catalog, certification_prerequisites, and now
certification_drives, drive_certifications, drive_enrollments,
learning_path_items, knowledge_base) has no business-specific audit
nuance — they all just need "log old/new on every create/update/delete,
exactly once." Rather than hand-writing that per table (per the explicit
instruction not to), every one of those tables' endpoints calls the three
functions below, built on the same serialize_row/write_audit_entry
primitives services/audit.py already provides for
certification_catalog/certification_prerequisites.
"""

from typing import Any, TypeVar

from sqlalchemy.orm import Session

from app.services.audit import serialize_row, write_audit_entry

ModelT = TypeVar("ModelT")


def _differs(current: Any, new: Any) -> bool:
    """Plain `!=` raises ValueError ("truth value of an array is
    ambiguous") for pgvector embedding columns, which come back as numpy
    arrays. Callers only ever put an embedding in `changes` when
    services/rag_content.py's embed_if_changed() already determined the
    content hash changed — so there's no need to re-compare the vector
    itself here; just treat it as changed."""
    if hasattr(current, "tolist") or hasattr(new, "tolist"):
        return True
    return current != new


def create_with_audit(db: Session, model_cls: type[ModelT], *, actor: str, **fields: Any) -> ModelT:
    instance = model_cls(**fields)
    db.add(instance)
    db.flush()  # assign PK before audit-logging it
    write_audit_entry(
        db,
        entity_type=instance.__tablename__,
        entity_id=instance.id,
        action="created",
        actor=actor,
        old_value=None,
        new_value=serialize_row(instance),
    )
    return instance


def update_with_audit(
    db: Session,
    instance: ModelT,
    *,
    actor: str,
    changes: dict[str, Any],
    action_override: str | None = None,
) -> ModelT:
    """Sets each field in `changes`, but only writes an audit entry (and
    only touches the row at all) if something actually changed — a save
    that sets every field to its current value is a no-op, not a
    field_updated entry."""
    old = serialize_row(instance)
    changed = False
    for key, value in changes.items():
        if _differs(getattr(instance, key), value):
            setattr(instance, key, value)
            changed = True

    if not changed:
        return instance

    db.flush()
    write_audit_entry(
        db,
        entity_type=instance.__tablename__,
        entity_id=instance.id,
        action=action_override or "field_updated",
        actor=actor,
        old_value=old,
        new_value=serialize_row(instance),
    )
    return instance


def delete_with_audit(db: Session, instance: ModelT, *, actor: str) -> None:
    """Hard delete. Only ever call this for rows where nothing else
    references them (see routers/drives.py delete_drive's zero-enrollment
    guard) — anything with real history should be soft-deleted (a status
    flag) via update_with_audit instead, so the audit trail's own record of
    what happened is never itself the only casualty of a delete."""
    entity_type = instance.__tablename__
    entity_id = instance.id
    old_value = serialize_row(instance)
    db.delete(instance)
    db.flush()
    write_audit_entry(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        action="deleted",
        actor=actor,
        old_value=old_value,
        new_value=None,
    )
