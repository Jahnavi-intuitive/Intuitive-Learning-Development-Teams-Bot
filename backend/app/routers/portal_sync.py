"""Admin visibility + on-demand trigger for the live SharePoint ingestion
(app/services/portal_sharepoint_sync.py) — the "last successful sync per
provider" health signal called for in that module's design, plus a manual
trigger for testing without waiting for the scheduled interval.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Employee
from app.portal_db import get_portal_db
from app.portal_models import CertificationRecord, IngestionLog, Provider
from app.services.admin_auth import require_admin
from app.services.portal_sharepoint_sync import run_sync

router = APIRouter(prefix="/api/admin/portal/sync", tags=["portal-admin"])


class SyncRunOut(BaseModel):
    providers_synced: list[str]
    rows_inserted: int
    rows_updated: int
    rows_deleted: int
    rows_rejected: int
    error: str | None


class ProviderSyncStatusOut(BaseModel):
    provider: str
    last_synced_at: datetime | None
    record_count: int


class SyncStatusOut(BaseModel):
    last_run_started_at: datetime | None
    last_run_finished_at: datetime | None
    last_run_trigger: str | None
    last_run_error: str | None
    providers: list[ProviderSyncStatusOut]


@router.post("/run-now", response_model=SyncRunOut)
def trigger_sync_now(admin: Employee = Depends(require_admin)):
    """Runs synchronously in the request — these 4 files are small (a few
    hundred rows total), a few seconds at most, same as the main app's
    existing /api/admin/sync-tracker on-demand pattern."""
    summary = run_sync(trigger="manual")
    return SyncRunOut(
        providers_synced=summary.providers_synced,
        rows_inserted=summary.rows_inserted,
        rows_updated=summary.rows_updated,
        rows_deleted=summary.rows_deleted,
        rows_rejected=summary.rows_rejected,
        error=summary.error,
    )


@router.get("/status", response_model=SyncStatusOut)
def sync_status(
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    last_run = portal_db.execute(select(IngestionLog).order_by(IngestionLog.id.desc()).limit(1)).scalar_one_or_none()

    per_provider = dict(
        portal_db.execute(
            select(CertificationRecord.provider, func.max(CertificationRecord.last_synced_at)).group_by(
                CertificationRecord.provider
            )
        ).all()
    )
    counts = dict(
        portal_db.execute(select(CertificationRecord.provider, func.count()).group_by(CertificationRecord.provider)).all()
    )

    return SyncStatusOut(
        last_run_started_at=last_run.started_at if last_run else None,
        last_run_finished_at=last_run.finished_at if last_run else None,
        last_run_trigger=last_run.trigger if last_run else None,
        last_run_error=last_run.error if last_run else None,
        providers=[
            ProviderSyncStatusOut(provider=p.value, last_synced_at=per_provider.get(p), record_count=counts.get(p, 0))
            for p in Provider
        ],
    )
