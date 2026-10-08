"""Prep-time tracking for in-progress certifications.

An employee's prep clock starts at the record's prep_start_date (set to the
day they said they were starting — see routers/certifications.py
POST /start) and is measured in business days (Mon–Fri; public holidays
aren't modelled). Each certification level has a target number of business
days (notification_settings.prep_target_business_days, admin-adjustable);
past it, the reminder sweep starts nudging the employee
(services/notification_sweep.py::_prep_overdue_pass).

One shared implementation so the agent, the portal and the sweep can never
disagree about how many days someone has been preparing.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CertificateRecord, CertificationCatalog, Employee, NotificationSettings
from app.services.recommender.taxonomy import normalize_level

DEFAULT_TARGETS = {"foundational": 10, "associate": 20, "professional": 30, "specialty": 30, "expert": 40}


def business_days_between(start: date, end: date) -> int:
    """Business days elapsed from `start` (exclusive) to `end` (inclusive) —
    0 on the start day itself, 1 the next working day, and so on."""
    if end <= start:
        return 0
    days = (end - start).days
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(1, rest + 1):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def add_business_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def targets(db: Session) -> dict:
    settings = db.scalar(select(NotificationSettings).limit(1))
    configured = (settings.prep_target_business_days if settings else None) or {}
    return {**DEFAULT_TARGETS, **configured}


def level_for(db: Session, record: CertificateRecord) -> str:
    """The record's certification level on the shared ladder; unknown
    levels are treated as associate (the median tier)."""
    level_text = None
    if record.certification_id:
        level_text = db.scalar(select(CertificationCatalog.level).where(CertificationCatalog.id == record.certification_id))
    return normalize_level(level_text, record.cert_name) or "associate"


@dataclass
class Progress:
    record: CertificateRecord
    level: str
    business_days_elapsed: int | None
    target_business_days: int | None
    target_date: date | None
    overdue_business_days: int

    @property
    def is_overdue(self) -> bool:
        return self.overdue_business_days > 0


def progress_for(db: Session, record: CertificateRecord, today: date, target_map: dict | None = None) -> Progress:
    level = level_for(db, record)
    target_map = target_map or targets(db)
    if record.submission_status != "in_progress" or not record.prep_start_date:
        return Progress(record, level, None, None, None, 0)
    target = int(target_map.get(level, DEFAULT_TARGETS["associate"]))
    elapsed = business_days_between(record.prep_start_date, today)
    return Progress(record, level, elapsed, target, add_business_days(record.prep_start_date, target),
                    max(0, elapsed - target))


def latest_records(db: Session, employee: Employee) -> list[CertificateRecord]:
    """One record per certification for this employee — the most recent
    one — so a cert that was in progress and later completed shows once,
    as completed."""
    rows = db.scalars(
        select(CertificateRecord)
        .where(CertificateRecord.employee_id == employee.id)
        .order_by(CertificateRecord.created_at.desc())
    ).all()
    seen, out = set(), []
    for r in rows:
        key = r.certification_id or r.cert_name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
