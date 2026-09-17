"""Admin CRUD for provider_targets (app/portal_models.py) — the per-provider
certified-employee goal the portal Dashboard's leaderboard (Section 1 of
frontend/portal.html) shows progress against.

Lives in a genuinely awkward spot on purpose: the row being written is in
the *portal* database (lnd_portal, its own engine/session — app/portal_db.py),
but admin auth and the audit trail are both main-app concepts
(app.services.admin_auth, app.models.AuditLog in lnd_certification). Rather
than force provider_targets into services/audited_crud.py's generic
create/update/delete (which assumes a UUID `.id` primary key and a single
session that owns both the row and the audit write), this router does both
writes itself, one per database, in the same request:
- entity_id is a deterministic uuid5 of the provider name (there's no real
  UUID for a portal-DB row), kept stable so every edit to the same
  provider's target links together in the one audit trail.
- The portal write is committed before the audit write, specifically so a
  failure never leaves an audit entry describing a change that didn't
  actually land — the reverse (a real change with a missing audit entry,
  on the rarer case where the *audit* commit itself fails) is the accepted
  risk, since no cross-database transaction exists to make this atomic.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Employee
from app.portal_db import get_portal_db
from app.portal_models import Provider, ProviderTarget
from app.services.admin_auth import require_admin
from app.services.audit import write_audit_entry

router = APIRouter(prefix="/api/admin/portal/provider-targets", tags=["portal-admin"])

_ENTITY_TYPE = "provider_target"


def _entity_id(provider: Provider) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{_ENTITY_TYPE}:{provider.value}")


class ProviderTargetOut(BaseModel):
    provider: str
    target_count: int | None  # None = no target set yet
    updated_by: str | None
    updated_at: datetime | None


class ProviderTargetSetIn(BaseModel):
    target_count: int


@router.get("", response_model=list[ProviderTargetOut])
def list_provider_targets(
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    """Every provider in fixed catalog order, not just the ones with a
    target row already — the admin UI needs an editable field for every
    provider, including ones with nothing set yet."""
    existing = {row.provider: row for row in portal_db.scalars(select(ProviderTarget)).all()}
    return [
        ProviderTargetOut(
            provider=p.value,
            target_count=existing[p].target_count if p in existing else None,
            updated_by=existing[p].updated_by if p in existing else None,
            updated_at=existing[p].updated_at if p in existing else None,
        )
        for p in Provider
    ]


@router.put("/{provider}", response_model=ProviderTargetOut)
def set_provider_target(
    provider: Provider,
    payload: ProviderTargetSetIn,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    if payload.target_count < 0:
        raise HTTPException(status_code=422, detail="target_count must be >= 0.")

    row = portal_db.get(ProviderTarget, provider)
    old_value = {"target_count": row.target_count} if row else None
    is_create = row is None

    if row is None:
        row = ProviderTarget(provider=provider, target_count=payload.target_count, updated_by=admin.employee_id)
        portal_db.add(row)
    else:
        row.target_count = payload.target_count
        row.updated_by = admin.employee_id

    portal_db.commit()
    portal_db.refresh(row)

    write_audit_entry(
        db,
        entity_type=_ENTITY_TYPE,
        entity_id=_entity_id(provider),
        action="created" if is_create else "field_updated",
        actor=admin.employee_id,
        old_value=old_value,
        new_value={"target_count": row.target_count},
    )
    db.commit()

    return ProviderTargetOut(provider=row.provider.value, target_count=row.target_count, updated_by=row.updated_by, updated_at=row.updated_at)


@router.delete("/{provider}", status_code=204)
def clear_provider_target(
    provider: Provider,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    """Back to "no target set" — a real, distinct state from any specific
    number (see ProviderTargetOut.target_count: None), not just 0."""
    row = portal_db.get(ProviderTarget, provider)
    if row is None:
        return None
    old_value = {"target_count": row.target_count}
    portal_db.delete(row)
    portal_db.commit()

    write_audit_entry(
        db,
        entity_type=_ENTITY_TYPE,
        entity_id=_entity_id(provider),
        action="deleted",
        actor=admin.employee_id,
        old_value=old_value,
        new_value=None,
    )
    db.commit()
    return None
