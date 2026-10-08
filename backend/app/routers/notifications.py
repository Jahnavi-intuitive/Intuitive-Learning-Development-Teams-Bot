"""Admin surface for the reminder sweep: on-demand trigger, notification
listing, and the adjustable org-wide settings row. The sweep logic itself
lives entirely in services/notification_sweep.py — this router only calls
it and exposes what it did.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Employee, Notification, NotificationSettings
from app.schemas import (
    NotificationOut,
    NotificationSettingsOut,
    NotificationSettingsUpdateIn,
    SweepReportOut,
    SweepTriggerIn,
)
from app.services.access_scope import get_current_employee
from app.services.admin_auth import require_admin
from app.services.audited_crud import update_with_audit
from app.services.notification_sweep import run_sweep

router = APIRouter(prefix="/api/admin/notifications", tags=["notifications"])


@router.post("/run-sweep", response_model=SweepReportOut)
def trigger_sweep(
    payload: SweepTriggerIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """The exact same function the scheduled job calls (app.main's
    APScheduler wiring) — there is no separate "test" sweep implementation.
    `simulate_failure_employee_ids` is a test-only control for exercising
    the retry pass on demand; leave it empty for a normal run."""
    report = run_sweep(db, simulate_failure_employee_ids=payload.simulate_failure_employee_ids)
    db.commit()
    return SweepReportOut(**report.__dict__)


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    status: str | None = None,
    notification_type: str | None = None,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    stmt = select(Notification).options(selectinload(Notification.recipient))
    if status:
        stmt = stmt.where(Notification.status == status)
    if notification_type:
        stmt = stmt.where(Notification.notification_type == notification_type)
    stmt = stmt.order_by(Notification.created_at.desc())
    rows = db.scalars(stmt).all()
    return [
        NotificationOut(
            id=n.id,
            dedupe_key=n.dedupe_key,
            notification_type=n.notification_type,
            recipient_employee_id=n.recipient.employee_id,
            recipient_name=n.recipient.name,
            channel=n.channel,
            message=n.message,
            related_drive_id=n.related_drive_id,
            related_certificate_record_id=n.related_certificate_record_id,
            threshold_days=n.threshold_days,
            escalation_level=n.escalation_level,
            status=n.status,
            retry_count=n.retry_count,
            last_attempt_at=n.last_attempt_at,
            is_test_data=n.is_test_data,
            created_at=n.created_at,
        )
        for n in rows
    ]


# --- Employee-facing: the portal's Notifications tab -----------------------
# Every notification the sweep creates is also shown in-app here (the only
# delivery today; email is a later channel — services/notification_channel.py).
me_router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _mine_out(n: Notification) -> dict:
    return {
        "id": n.id,
        "notification_type": n.notification_type,
        "message": n.message,
        "threshold_days": n.threshold_days,
        "related_certificate_record_id": n.related_certificate_record_id,
        "created_at": n.created_at,
        "read_at": n.read_at,
    }


@me_router.get("/mine")
def my_notifications(
    employee_id: str,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only view your own notifications.")
    rows = db.scalars(
        select(Notification).where(Notification.recipient_employee_id == current.id)
        .order_by(Notification.created_at.desc()).limit(100)
    ).all()
    return {"notifications": [_mine_out(n) for n in rows], "unread": sum(1 for n in rows if n.read_at is None)}


@me_router.post("/mark-read")
def mark_my_notifications_read(
    employee_id: str,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only update your own notifications.")
    now = datetime.now(timezone.utc)
    unread = db.scalars(
        select(Notification).where(Notification.recipient_employee_id == current.id, Notification.read_at.is_(None))
    ).all()
    for n in unread:
        n.read_at = now
    db.commit()
    return {"marked_read": len(unread)}


@router.get("/settings", response_model=NotificationSettingsOut)
def get_notification_settings(db: Session = Depends(get_db), admin: Employee = Depends(require_admin)):
    return db.scalar(select(NotificationSettings).limit(1))


@router.patch("/settings", response_model=NotificationSettingsOut)
def update_notification_settings(
    payload: NotificationSettingsUpdateIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """The adjustable org-wide expiry-reminder windows + retry cap —
    BR-D-05 explicitly calls for these to not be hardcoded. There is
    always exactly one row (seeded by migration 0004); this updates it in
    place rather than ever inserting a second one."""
    settings = db.scalar(select(NotificationSettings).limit(1))
    changes = payload.model_dump(exclude_unset=True)
    update_with_audit(db, settings, actor=admin.employee_id, changes=changes)
    db.commit()
    db.refresh(settings)
    return settings
