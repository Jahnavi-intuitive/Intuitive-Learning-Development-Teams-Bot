"""APScheduler wiring for the reminder sweep — the local stand-in for the
BRD's own proposed production stack (Cloud Scheduler + Cloud Functions).
One BackgroundScheduler, one daily job, calling the exact same
services.notification_sweep.run_sweep() the admin dashboard's on-demand
trigger calls — there is no separate "scheduled" implementation to drift
out of sync with the on-demand one.
"""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import SessionLocal
from app.services.notification_sweep import run_sweep
from app.services.portal_sharepoint_sync import run_sync as run_portal_sync

logger = logging.getLogger("notifications")
portal_sync_logger = logging.getLogger("portal_sync")

_scheduler: BackgroundScheduler | None = None


def _run_sweep_job() -> None:
    """Runs in the scheduler's own thread, with its own DB session — the
    request-scoped `get_db` FastAPI dependency doesn't apply here since
    nothing is serving an HTTP request."""
    db = SessionLocal()
    try:
        report = run_sweep(db)
        db.commit()
        logger.info("Scheduled reminder sweep completed: %s", report)
    except Exception:
        db.rollback()
        logger.exception("Scheduled reminder sweep failed")
    finally:
        db.close()


def _run_portal_sync_job() -> None:
    """The delta-style reconciliation fallback for the live SharePoint
    ingestion (services/portal_sharepoint_sync.py) — real-time change
    notifications (Graph webhooks) need a public HTTPS endpoint this
    backend doesn't have yet (local-only), so this scheduled interval is
    the only sync mechanism for now, not a backstop for a webhook that
    doesn't exist. run_sync manages its own PortalSessionLocal, matching
    the fact that this is a physically separate database from the one
    SessionLocal above connects to."""
    try:
        summary = run_portal_sync(trigger="scheduled")
        if summary.error:
            portal_sync_logger.error("Scheduled portal SharePoint sync failed: %s", summary.error)
        else:
            portal_sync_logger.info(
                "Scheduled portal SharePoint sync completed: %s providers, +%d/~%d/-%d rows, %d rejected.",
                summary.providers_synced, summary.rows_inserted, summary.rows_updated,
                summary.rows_deleted, summary.rows_rejected,
            )
    except Exception:
        portal_sync_logger.exception("Scheduled portal SharePoint sync failed")


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler()

    if settings.notification_sweep_enabled:
        _scheduler.add_job(
            _run_sweep_job,
            trigger=CronTrigger(hour=settings.notification_sweep_hour, minute=settings.notification_sweep_minute),
            id="reminder_sweep",
            replace_existing=True,
        )
        logger.info(
            "Reminder sweep scheduled daily at %02d:%02d local time.",
            settings.notification_sweep_hour,
            settings.notification_sweep_minute,
        )
    else:
        logger.info("Reminder sweep scheduling disabled (notification_sweep_enabled=false).")

    if settings.portal_sync_enabled:
        _scheduler.add_job(
            _run_portal_sync_job,
            trigger=IntervalTrigger(minutes=settings.portal_sync_interval_minutes),
            id="portal_sharepoint_sync",
            replace_existing=True,
            next_run_time=datetime.now(),  # also run once immediately at startup
        )
        portal_sync_logger.info(
            "Portal SharePoint sync scheduled every %d minute(s).", settings.portal_sync_interval_minutes
        )
    else:
        portal_sync_logger.info("Portal SharePoint sync scheduling disabled (portal_sync_enabled=false).")

    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
