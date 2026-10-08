"""The reminder sweep — a scheduled scan, not a per-record task queue.

Design decision (see DEVELOPMENT_LOG.md's 2026-08-25 "reminder sweep"
entry for the full reasoning): a single job runs on a fixed schedule
(APScheduler locally; Cloud Scheduler + Cloud Functions later), scans
certification_drives/drive_enrollments and certificate_records each run,
and decides what to send — rather than enqueuing a discrete future task
per reminder at record-creation time, which would need canceling/
rescheduling every time a deadline changes, a cert renews, or a drive
closes early. This system's actual scale (one company's headcount, a
handful of active drives) doesn't justify that complexity.

Idempotency comes from the database, not application bookkeeping:
`notifications.dedupe_key` has a unique constraint, and every candidate
this module builds is inserted via `INSERT ... ON CONFLICT (dedupe_key)
DO NOTHING RETURNING id` — only rows that actually get inserted are new,
and only those get an actual (simulated) send. Running the sweep twice
for the same day is a no-op the second time: every candidate it builds
already exists, so nothing new gets inserted or sent.

"Just crossed a threshold" is evaluated as "days_remaining <= threshold",
not "days_remaining == threshold" — a job that misses a day (crashes,
skipped) still catches up on any threshold it missed, rather than
silently never firing it. The unique dedupe_key per (subject, threshold)
is what stops that from ever double-sending once it's caught up.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from app.models import (
    CertificateRecord,
    CertificationCatalog,
    CertificationDrive,
    DriveCertification,
    DriveEnrollment,
    Employee,
    Notification,
    NotificationSettings,
)
from app.services import cert_progress
from app.services.audit import serialize_row, write_audit_entry
from app.services.audited_crud import update_with_audit
from app.services.notification_channel import send_notification


@dataclass
class _Candidate:
    dedupe_key: str
    notification_type: str
    recipient: Employee
    message: str
    related_drive_id: uuid.UUID | None = None
    related_certificate_record_id: uuid.UUID | None = None
    threshold_days: int | None = None
    escalation_level: str | None = None
    is_test_data: bool = False


@dataclass
class SweepReport:
    drive_reminders_created: int = 0
    drive_escalations_created: int = 0
    cert_expiry_reminders_created: int = 0
    prep_overdue_reminders_created: int = 0
    skipped_duplicate: int = 0
    escalations_skipped_unresolved_recipient: int = 0
    sent: int = 0
    failed: int = 0
    retried: int = 0
    retried_now_sent: int = 0
    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _get_settings(db: Session) -> NotificationSettings:
    settings = db.scalar(select(NotificationSettings).limit(1))
    if not settings:
        raise RuntimeError(
            "notification_settings has no row — migration 0004 should have seeded exactly one."
        )
    return settings


def _resolve_employee_by_name(db: Session, name: str | None) -> Employee | None:
    """Best-effort: the local employees table is a small stand-in for the
    real HRIS, so a manager/practice director named on someone's record
    may not themselves have an employees row (common with only a handful
    of test employees seeded). When that happens the escalation candidate
    is skipped, not guessed at — see escalations_skipped_unresolved_recipient
    in SweepReport."""
    if not name:
        return None
    return db.scalar(select(Employee).where(Employee.name == name))


def _drive_pass(db: Session, today: date) -> tuple[list[_Candidate], int]:
    """Every active drive, every enrollment not yet completed/exempted:
    one employee-reminder candidate per reminder_days threshold already
    reached, plus one escalation candidate per escalation rule already
    reached. Returns (candidates, skipped_unresolved_count)."""
    candidates: list[_Candidate] = []
    skipped_unresolved = 0

    drives = db.scalars(select(CertificationDrive).where(CertificationDrive.status == "active")).all()
    for drive in drives:
        cadence = drive.reminder_cadence or {}
        reminder_days = sorted(cadence.get("reminder_days") or [], reverse=True)
        escalation_rules = cadence.get("escalation") or []
        if not reminder_days and not escalation_rules:
            continue

        days_remaining = (drive.deadline - today).days

        enrollments = db.scalars(
            select(DriveEnrollment)
            .where(
                DriveEnrollment.drive_id == drive.id,
                DriveEnrollment.enrollment_status.notin_(["completed", "exempted"]),
            )
            .options(selectinload(DriveEnrollment.employee))
        ).all()
        if not enrollments:
            continue

        required_cert_names = [
            c.name
            for c in db.scalars(
                select(CertificationCatalog)
                .join(DriveCertification, DriveCertification.certification_id == CertificationCatalog.id)
                .where(DriveCertification.drive_id == drive.id)
            )
        ]
        certs_str = ", ".join(required_cert_names) or "the required certification(s)"

        for enrollment in enrollments:
            employee = enrollment.employee
            for threshold in reminder_days:
                if days_remaining <= threshold:
                    message = (
                        f'Reminder: "{drive.name}" requires {certs_str}, due {drive.deadline.isoformat()} '
                        f"({days_remaining} day(s) remaining). Please complete this if you haven't already."
                    )
                    candidates.append(
                        _Candidate(
                            dedupe_key=f"drive:{drive.id}:{employee.employee_id}:{threshold}",
                            notification_type="drive_reminder",
                            recipient=employee,
                            message=message,
                            related_drive_id=drive.id,
                            threshold_days=threshold,
                            escalation_level="employee",
                            is_test_data=drive.is_test_data or employee.is_test_data,
                        )
                    )

            for rule in escalation_rules:
                threshold = rule.get("threshold_days")
                escalate_to = rule.get("escalate_to")
                if threshold is None or escalate_to not in ("manager", "practice_director"):
                    continue
                if days_remaining > threshold:
                    continue
                target_name = (
                    employee.manager_name if escalate_to == "manager" else employee.practice_director_name
                )
                target = _resolve_employee_by_name(db, target_name)
                if not target:
                    skipped_unresolved += 1
                    continue
                message = (
                    f'Escalation: {employee.name} has not completed "{drive.name}" '
                    f"({certs_str}), due {drive.deadline.isoformat()} ({days_remaining} day(s) remaining)."
                )
                candidates.append(
                    _Candidate(
                        dedupe_key=f"drive_escalation:{drive.id}:{employee.employee_id}:{threshold}:{escalate_to}",
                        notification_type="drive_escalation",
                        recipient=target,
                        message=message,
                        related_drive_id=drive.id,
                        threshold_days=threshold,
                        escalation_level=escalate_to,
                        is_test_data=drive.is_test_data or employee.is_test_data,
                    )
                )

    return candidates, skipped_unresolved


def _cert_expiry_pass(db: Session, today: date, expiry_windows: list[int]) -> list[_Candidate]:
    """Only the LATEST verified record per (employee, certification) is
    considered — a renewal (a newer verified record with a later
    expiration_date) supersedes the old one, which then naturally stops
    generating candidates since it's no longer "the latest.\""""
    if not expiry_windows:
        return []

    records = db.scalars(
        select(CertificateRecord).where(
            CertificateRecord.expiration_date.isnot(None),
            CertificateRecord.verification_status == "verified",
        )
    ).all()

    latest_by_group: dict[tuple, CertificateRecord] = {}
    for r in records:
        if not r.employee_id or not r.certification_id:
            continue
        key = (r.employee_id, r.certification_id)
        existing = latest_by_group.get(key)
        if not existing or r.expiration_date > existing.expiration_date:
            latest_by_group[key] = r

    candidates: list[_Candidate] = []
    for record in latest_by_group.values():
        employee = db.get(Employee, record.employee_id)
        if not employee:
            continue
        days_to_expiry = (record.expiration_date - today).days
        for threshold in sorted(expiry_windows, reverse=True):
            if days_to_expiry <= threshold:
                message = (
                    f'Your certification "{record.cert_name}" expires on '
                    f"{record.expiration_date.isoformat()} ({days_to_expiry} day(s) from today). "
                    "Please plan to renew it."
                )
                candidates.append(
                    _Candidate(
                        dedupe_key=f"cert_expiry:{record.id}:{threshold}",
                        notification_type="cert_expiry_reminder",
                        recipient=employee,
                        message=message,
                        related_certificate_record_id=record.id,
                        threshold_days=threshold,
                        escalation_level="employee",
                        is_test_data=employee.is_test_data,
                    )
                )
    return candidates


def _prep_overdue_pass(db: Session, today: date) -> list[_Candidate]:
    """In-progress certifications whose prep has run past their level's
    business-day target (services/cert_progress.py). One reminder when the
    target is first passed, then one more every further 5 business days
    (a working week) still overdue — the bucket in the dedupe_key is what
    makes that a weekly nudge rather than a daily one. Only each
    employee's latest record per certification counts, so a cert they've
    since completed never gets nagged about."""
    target_map = cert_progress.targets(db)
    in_progress = db.scalars(
        select(CertificateRecord.employee_id).where(
            CertificateRecord.submission_status == "in_progress",
            CertificateRecord.prep_start_date.isnot(None),
            CertificateRecord.employee_id.isnot(None),
        ).distinct()
    ).all()

    candidates: list[_Candidate] = []
    for emp_id in in_progress:
        employee = db.get(Employee, emp_id)
        if not employee:
            continue
        for record in cert_progress.latest_records(db, employee):
            p = cert_progress.progress_for(db, record, today, target_map)
            if not p.is_overdue:
                continue
            bucket = (p.overdue_business_days - 1) // 5
            first = (employee.name or "").split(" ")[0] or "there"
            message = (
                f"Hi {first}, you started preparing for \"{record.cert_name}\" on "
                f"{record.prep_start_date.strftime('%d %b %Y')} — that's {p.business_days_elapsed} business days, "
                f"past the {p.target_business_days}-business-day target for "
                f"{'an' if p.level[:1] in 'aeiou' else 'a'} {p.level} certification. "
                "Please book your exam and finish it, or update its status if your plans have changed."
            )
            candidates.append(
                _Candidate(
                    dedupe_key=f"prep_overdue:{record.id}:{bucket}",
                    notification_type="prep_overdue_reminder",
                    recipient=employee,
                    message=message,
                    related_certificate_record_id=record.id,
                    threshold_days=p.target_business_days,
                    escalation_level="employee",
                    is_test_data=employee.is_test_data,
                )
            )
    return candidates


def _insert_and_send(
    db: Session, candidates: list[_Candidate], simulate_failure_employee_ids: set[str]
) -> list[tuple[_Candidate, bool | None]]:
    """Returns one (candidate, outcome) pair per input candidate:
    outcome is None if it was a duplicate (already existed — skipped),
    True if sent successfully just now, False if the send failed just
    now. This is the ONLY place idempotency is actually enforced — via
    the dedupe_key unique constraint, not by checking for existence
    first (which would race against a concurrent run)."""
    if not candidates:
        return []

    now = datetime.now(timezone.utc)
    rows = []
    by_dedupe_key: dict[str, _Candidate] = {}
    id_by_dedupe_key: dict[str, uuid.UUID] = {}
    for c in candidates:
        new_id = uuid.uuid4()
        by_dedupe_key[c.dedupe_key] = c
        id_by_dedupe_key[c.dedupe_key] = new_id
        rows.append(
            {
                "id": new_id,
                "dedupe_key": c.dedupe_key,
                "notification_type": c.notification_type,
                "recipient_employee_id": c.recipient.id,
                "channel": "teams",
                "message": c.message,
                "related_drive_id": c.related_drive_id,
                "related_certificate_record_id": c.related_certificate_record_id,
                "threshold_days": c.threshold_days,
                "escalation_level": c.escalation_level,
                "status": "pending",
                "retry_count": 0,
                "is_test_data": c.is_test_data,
            }
        )

    if db.get_bind().dialect.name == "postgresql":
        stmt = (
            pg_insert(Notification)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(Notification.id, Notification.dedupe_key)
        )
        inserted = db.execute(stmt).all()
    else:
        # Databricks doesn't enforce unique constraints, so skip already-sent
        # dedupe keys explicitly. Safe because the sweep only ever runs in the
        # backend's single process (one container replica).
        existing = set(
            db.execute(select(Notification.dedupe_key).where(Notification.dedupe_key.in_(list(by_dedupe_key)))).scalars()
        )
        new_rows = [r for r in rows if r["dedupe_key"] not in existing]
        for r in new_rows:
            db.add(Notification(**r))
        inserted = [(r["id"], r["dedupe_key"]) for r in new_rows]
    db.flush()
    inserted_dedupe_keys = {dedupe_key for _id, dedupe_key in inserted}

    results: list[tuple[_Candidate, bool | None]] = []
    for notif_id, dedupe_key in inserted:
        candidate = by_dedupe_key[dedupe_key]
        instance = db.get(Notification, notif_id)
        write_audit_entry(
            db,
            entity_type="notifications",
            entity_id=instance.id,
            action="created",
            actor="system",
            old_value=None,
            new_value=serialize_row(instance),
        )
        force_fail = candidate.recipient.employee_id in simulate_failure_employee_ids
        ok = send_notification(candidate.recipient, candidate.message, force_fail=force_fail)
        update_with_audit(
            db,
            instance,
            actor="system",
            changes={"status": "sent" if ok else "failed", "last_attempt_at": now},
            action_override="status_changed",
        )
        results.append((candidate, ok))

    for c in candidates:
        if c.dedupe_key not in inserted_dedupe_keys:
            results.append((c, None))

    return results


def _retry_pass(db: Session, retry_cap: int, simulate_failure_employee_ids: set[str]) -> tuple[int, int]:
    """Separate from the two build passes, deliberately — this is just
    another query in the same job, not a queue: everything with
    status='failed' and retry_count below the cap gets one more attempt
    per sweep run."""
    now = datetime.now(timezone.utc)
    failed_notifs = db.scalars(
        select(Notification)
        .where(Notification.status == "failed", Notification.retry_count < retry_cap)
        .options(selectinload(Notification.recipient))
    ).all()

    retried = now_sent = 0
    for notif in failed_notifs:
        force_fail = notif.recipient.employee_id in simulate_failure_employee_ids
        ok = send_notification(notif.recipient, notif.message, force_fail=force_fail)
        update_with_audit(
            db,
            notif,
            actor="system",
            changes={
                "status": "sent" if ok else "failed",
                "retry_count": notif.retry_count + 1,
                "last_attempt_at": now,
            },
            action_override="status_changed",
        )
        retried += 1
        if ok:
            now_sent += 1
    return retried, now_sent


def run_sweep(
    db: Session,
    *,
    today: date | None = None,
    simulate_failure_employee_ids: list[str] | None = None,
) -> SweepReport:
    """The one entrypoint — called by the APScheduler job and by the
    admin dashboard's on-demand trigger, so there is exactly one
    implementation of the sweep, never a duplicated "test version.\""""
    today = today or datetime.now(timezone.utc).date()
    fail_set = set(simulate_failure_employee_ids or [])
    settings = _get_settings(db)

    drive_candidates, skipped_unresolved = _drive_pass(db, today)
    cert_candidates = _cert_expiry_pass(db, today, settings.expiry_reminder_days)
    prep_candidates = _prep_overdue_pass(db, today)

    results = _insert_and_send(db, drive_candidates + cert_candidates + prep_candidates, fail_set)
    retried, retried_now_sent = _retry_pass(db, settings.retry_cap, fail_set)

    report = SweepReport(escalations_skipped_unresolved_recipient=skipped_unresolved)
    for candidate, outcome in results:
        if outcome is None:
            report.skipped_duplicate += 1
            continue
        if candidate.notification_type == "drive_reminder":
            report.drive_reminders_created += 1
        elif candidate.notification_type == "drive_escalation":
            report.drive_escalations_created += 1
        elif candidate.notification_type == "cert_expiry_reminder":
            report.cert_expiry_reminders_created += 1
        elif candidate.notification_type == "prep_overdue_reminder":
            report.prep_overdue_reminders_created += 1
        if outcome:
            report.sent += 1
        else:
            report.failed += 1

    report.retried = retried
    report.retried_now_sent = retried_now_sent
    return report
