"""APScheduler wiring for the reminder sweep — the local stand-in for the
BRD's own proposed production stack (Cloud Scheduler + Cloud Functions).
One BackgroundScheduler, one daily job, calling the exact same
services.notification_sweep.run_sweep() the admin dashboard's on-demand
trigger calls — there is no separate "scheduled" implementation to drift
out of sync with the on-demand one.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import SessionLocal
from app.services.notification_sweep import run_sweep

logger = logging.getLogger("notifications")

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


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.notification_sweep_enabled:
        logger.info("Reminder sweep scheduling disabled (notification_sweep_enabled=false).")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _run_sweep_job,
        trigger=CronTrigger(hour=settings.notification_sweep_hour, minute=settings.notification_sweep_minute),
        id="reminder_sweep",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Reminder sweep scheduled daily at %02d:%02d local time.",
        settings.notification_sweep_hour,
        settings.notification_sweep_minute,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
