"""Leadership dashboards — 2026-09-14, built on direct request after the
user's demo-org screenshot showed a real PD/Leader x Status pivot table
their org already uses. Two distinct audiences, per direct instruction:

- The "manager" dashboard isn't a new endpoint at all: routers/tracker.py's
  existing /api/tracker/records already scopes by resolve_scope(), and
  that scope was just fixed (services/access_scope.py) to walk the full
  manager_name chain recursively rather than stopping at direct reports —
  so a manager now sees indirect reports too, through the exact same
  endpoint that already existed. Nothing new needed here beyond that fix.
- This router covers the org-wide / general leadership view specifically,
  served by the standalone frontend/leadership.html page. Per direct
  request, the three read endpoints below are intentionally open (no
  auth/admin gate) — this is a live, no-login leadership screen, not an
  admin action. The one mutation this router exposes (writing the pivot
  into SharePoint) stays admin-gated, since that's a write, not a read.

Three pieces, matching the three explicitly requested:
1. Org-wide certification health, by practice.
2. Drive compliance + escalation-ladder standing, across every active drive.
3. Per-provider, per-certification PD/Leader x Status pivot (the shape of
   the user's own screenshot) — computed here, and also written into each
   provider's real SharePoint workbook as a "Summary" sheet via
   services/excel_sync.py::write_pd_pivot_summary_sheet.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificateRecord, CertificationCatalog, CertificationDrive, Employee, Notification
from app.services import excel_sync
from app.services.admin_auth import require_admin
from app.services.drive_compliance import compute_drive_compliance_summary
from app.services.pd_pivot import STATUS_DISPLAY, compute_pd_pivot_for_certification

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])


class PracticeCertHealthOut(BaseModel):
    practice: str
    total: int
    by_submission_status: dict[str, int]
    by_verification_status: dict[str, int]


class OrgCertHealthOut(BaseModel):
    practices: list[PracticeCertHealthOut]
    unassigned: PracticeCertHealthOut  # records whose employee has no practice set (e.g. auto-provisioned)
    data_as_of: datetime


@router.get("/org-cert-health", response_model=OrgCertHealthOut)
def org_cert_health(db: Session = Depends(get_db)):
    """Org-wide certification counts, broken down by practice — the
    cross-practice comparison view. Reads practice directly off
    certificate_records (the snapshot at submission time), not the
    employee's current practice, matching how every other tracker read in
    this system already treats certificate_records as the record of truth
    for what was true when the record was written."""
    rows = db.execute(
        select(CertificateRecord.practice, CertificateRecord.submission_status, CertificateRecord.verification_status)
    ).all()

    by_practice: dict[str, PracticeCertHealthOut] = {}
    for practice, submission_status, verification_status in rows:
        key = practice or "__unassigned__"
        if key not in by_practice:
            by_practice[key] = PracticeCertHealthOut(
                practice=practice or "Unassigned", total=0, by_submission_status={}, by_verification_status={}
            )
        entry = by_practice[key]
        entry.total += 1
        entry.by_submission_status[submission_status] = entry.by_submission_status.get(submission_status, 0) + 1
        entry.by_verification_status[verification_status] = (
            entry.by_verification_status.get(verification_status, 0) + 1
        )

    unassigned = by_practice.pop("__unassigned__", PracticeCertHealthOut(
        practice="Unassigned", total=0, by_submission_status={}, by_verification_status={}
    ))
    return OrgCertHealthOut(
        practices=sorted(by_practice.values(), key=lambda p: p.practice),
        unassigned=unassigned,
        data_as_of=datetime.now(timezone.utc),
    )


class DriveComplianceRowOut(BaseModel):
    drive_id: str
    name: str
    target_scope: str
    target_scope_value: str | None
    deadline: str
    status: str
    enrolled_count: int
    compliant_count: int
    exempted_count: int
    compliance_pct: float
    escalation_counts: dict[str, int]  # escalation_level -> distinct-enrollment count


class DriveComplianceOut(BaseModel):
    drives: list[DriveComplianceRowOut]
    data_as_of: datetime


@router.get("/drive-compliance", response_model=DriveComplianceOut)
def drive_compliance(db: Session = Depends(get_db)):
    """Every non-closed drive's live compliance % (same computation
    routers/drives.py's own get_drive_detail uses — never a second,
    divergent formula) plus how many enrollments currently sit at each
    escalation_level, org-wide."""
    drives = db.scalars(
        select(CertificationDrive).where(CertificationDrive.status != "closed")
    ).all()
    rows = [DriveComplianceRowOut(**compute_drive_compliance_summary(db, drive)) for drive in drives]
    return DriveComplianceOut(drives=rows, data_as_of=datetime.now(timezone.utc))


class CertPivotOut(BaseModel):
    certification_name: str
    short_name: str | None
    provider: str
    rows: dict[str, dict[str, int]]  # pd_name -> {status_display: count}
    grand_total: int


class ProviderPivotOut(BaseModel):
    provider: str
    certifications: list[CertPivotOut]


class PdPivotOut(BaseModel):
    providers: list[ProviderPivotOut]
    status_columns: list[str]
    data_as_of: datetime


@router.get("/pd-pivot", response_model=PdPivotOut)
def pd_pivot(db: Session = Depends(get_db)):
    """Per-provider, per-certification PD/Leader x Status pivot — the
    shape of the reference screenshot: rows are each certification's
    practice_director_name (never the employee's manager — this rolls up
    to the practice's director, per the seeded org hierarchy), columns are
    submission_status, one block per certification, grouped by provider."""
    catalog_entries = db.scalars(
        select(CertificationCatalog).where(CertificationCatalog.provider.is_not(None)).order_by(
            CertificationCatalog.provider, CertificationCatalog.name
        )
    ).all()

    providers: dict[str, list[CertPivotOut]] = {}
    for entry in catalog_entries:
        pivot_rows = compute_pd_pivot_for_certification(db, entry)
        if not pivot_rows:
            continue  # no certificate_records for this cert at all yet — nothing to show
        grand_total = sum(sum(status_counts.values()) for status_counts in pivot_rows.values())
        providers.setdefault(entry.provider, []).append(
            CertPivotOut(
                certification_name=entry.name,
                short_name=entry.short_name,
                provider=entry.provider,
                rows=pivot_rows,
                grand_total=grand_total,
            )
        )

    return PdPivotOut(
        providers=[ProviderPivotOut(provider=p, certifications=certs) for p, certs in sorted(providers.items())],
        status_columns=list(STATUS_DISPLAY.values()),
        data_as_of=datetime.now(timezone.utc),
    )


@router.post("/pd-pivot/write-to-sharepoint")
def write_pd_pivot_to_sharepoint(db: Session = Depends(get_db), admin: Employee = Depends(require_admin)):
    """Explicit admin action (mirrors POST /api/admin/sync-tracker's own
    on-demand-trigger pattern, not something that runs on every dashboard
    page load) — writes a "Summary" sheet into every provider's real
    SharePoint workbook containing that provider's PD/Leader x Status
    pivot for each of its certifications, per direct request that the
    pivot be stored in the sheet, not just shown in the dashboard."""
    providers = db.scalars(
        select(CertificationCatalog.provider).where(CertificationCatalog.provider.is_not(None)).distinct()
    ).all()
    written = []
    for provider in providers:
        entries = db.scalars(
            select(CertificationCatalog)
            .where(CertificationCatalog.provider == provider)
            .order_by(CertificationCatalog.name)
        ).all()
        blocks = []
        for entry in entries:
            pivot_rows = compute_pd_pivot_for_certification(db, entry)
            if pivot_rows:
                blocks.append((entry, pivot_rows))
        if blocks:
            excel_sync.write_pd_pivot_summary_sheet(provider, blocks)
            written.append(provider)
    return {"providers_updated": written}
