"""Shared "a certification name that isn't in the catalog yet was just
seen" handling.

Two call sites, previously divergent (a real gap closed 2026-09-13):
- services/excel_sync.py: a name found in a synced tracker spreadsheet row,
  keyed primarily by source_sheet_id (a hidden per-tab GUID) — that lookup
  stays in excel_sync.py, since it's specific to spreadsheet-tab identity,
  but it now delegates here once it needs to resolve/create by name alone.
- routers/certifications.py: an employee submitting a certification under
  a name that doesn't match anything. Before this existed, that path
  didn't create a catalog entry at all — the CertificateRecord just sat
  with certification_id=NULL forever, silently different behavior from
  the Excel-sync path for what's conceptually the same event.

get_or_create_by_name is now the one place a brand-new certification
actually gets provisioned: it assigns short_name (services/short_name.py —
the real Excel sheet name this cert will use, once the SharePoint-per-
provider-file work is live) and notifies every admin, exactly once, no
matter which of the two call sites triggered it.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CertificationCatalog, Employee, Notification
from app.services.short_name import generate_short_name


def get_or_create_by_name(db: Session, name: str) -> tuple[CertificationCatalog, bool]:
    """Returns (entry, created). `created=True` means this call just
    provisioned a brand-new pending_setup entry (short_name assigned,
    admins notified) — callers that also know a source_sheet_id or similar
    context-specific field still need to set it themselves afterward."""
    name = name.strip()
    existing = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == name))
    if existing:
        return existing, False

    existing_short_names = set(
        db.scalars(
            select(CertificationCatalog.short_name).where(CertificationCatalog.short_name.is_not(None))
        ).all()
    )
    short_name = generate_short_name(name, existing_short_names)

    entry = CertificationCatalog(name=name, short_name=short_name, status="pending_setup")
    db.add(entry)
    db.flush()  # assign the PK before referencing it in a notification
    _notify_admins_of_new_certification(db, entry)
    return entry, True


def _notify_admins_of_new_certification(db: Session, entry: CertificationCatalog) -> None:
    admins = db.scalars(select(Employee).where(Employee.is_admin.is_(True))).all()
    message = (
        f'A new certification "{entry.name}" (sheet name: {entry.short_name}) was just added and '
        "needs setup — provider, level, prerequisites, and validity period are still unset."
    )
    for admin in admins:
        db.add(
            Notification(
                dedupe_key=f"new_certification_added:{entry.id}:{admin.id}",
                notification_type="new_certification_added",
                recipient_employee_id=admin.id,
                channel="teams",
                message=message,
            )
        )
    db.flush()
