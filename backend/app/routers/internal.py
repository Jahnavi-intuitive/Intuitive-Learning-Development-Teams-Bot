"""Endpoints meant to be called by GCP infrastructure, not a person or the
agent — currently just the Cloud Scheduler sweep trigger.

Cloud Run's built-in IAM auth (`--no-allow-unauthenticated` + granting the
caller's service account `roles/run.invoker`) is per-*service*, not
per-route, so it can't protect just this one path on a service that also
serves the public employee/admin API. Protected instead with a shared
secret Cloud Scheduler is configured to send as a header — the same
pattern as a webhook signing secret. Disabled by default (empty
sweep_trigger_secret refuses every request); local dev and manual testing
use the admin-gated POST /api/admin/notifications/run-sweep instead.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas import SweepReportOut
from app.services.notification_sweep import run_sweep

router = APIRouter(prefix="/internal", tags=["internal"])


def _require_sweep_secret(x_sweep_trigger_secret: str | None = Header(default=None)) -> None:
    if not settings.sweep_trigger_secret:
        raise HTTPException(status_code=503, detail="Sweep trigger secret is not configured on this server.")
    if not x_sweep_trigger_secret or not hmac.compare_digest(x_sweep_trigger_secret, settings.sweep_trigger_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing sweep trigger secret.")


@router.post("/notifications/run-sweep", response_model=SweepReportOut, dependencies=[Depends(_require_sweep_secret)])
def cloud_scheduler_run_sweep(db: Session = Depends(get_db)):
    report = run_sweep(db)
    db.commit()
    return SweepReportOut(**report.__dict__)
