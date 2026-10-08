"""Data endpoints for the L&D portal Dashboard tab (frontend/portal.html)
— reads exclusively from the lnd_portal database (app/portal_db.py,
app/portal_models.py), the real Anthropic/Gemini/AWS/Databricks certification
data ingested by scripts/ingest_real_excel_data.py, plus the 4 catalog
providers with no ingested data yet (Azure/Bedrock/Google — shown
as a real, honest 0, not hidden). Deliberately open, no auth gate: matches
routers/dashboards.py's leadership-dashboard precedent (a live, no-login
read-only screen), and this router only ever reads. (Writing a provider's
target is an admin action — see routers/portal_admin.py instead.)

One combined `/overview` endpoint powers all three dashboard sections in a
single request/query round-trip: the frontend never re-queries when a viewer
switches the provider drill-down tab, it just re-filters the `certifications`
and `matrix` arrays already on the page — see that endpoint's docstring.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.portal_db import get_portal_db
from app.portal_models import CertificationRecord, Provider, ProviderTarget, StatusCanonical

router = APIRouter(prefix="/api/portal/dashboard", tags=["portal-dashboard"])

# "Unique employee, not certificate row" (explicit requirement everywhere in
# this router): none of the 4 source files share a reliable join key, so
# identity is the same normalized-name key scripts/ingest_real_excel_data.py's
# own dedupe_records() already uses — not email, since it's inconsistently
# populated (AWS never captures it) and at least one row is known to carry
# the wrong person's email by copy-paste error.
_IDENTITY = func.lower(func.trim(CertificationRecord.person_name))

# Certificates-under-provider status pill threshold. Our ingested data has no
# separate "verified via Credly" sub-status the way the main app's
# CertificateRecord does — status_canonical is just the tracker's own
# Certified/In Progress/Attempted-Not-Cleared. "Pending" below is read as
# in_progress; on_track is completed's share of (completed + pending),
# confirmed as the real business rule (not guessed) rather than the main
# app's verified/unverified distinction, which this data can't support.
_ON_TRACK_THRESHOLD = 0.8


class ProviderLeaderboardOut(BaseModel):
    provider: str
    completed: int
    target: int | None  # None = no target set yet (see routers/portal_admin.py)


class PracticeMetaOut(BaseModel):
    practice: str
    pd_leader_name: str | None  # most common non-null pd_leader_name on file for this practice
    headcount: int  # distinct people tracked in this practice, any provider/status — see docstring below


class MatrixCellOut(BaseModel):
    provider: str
    practice: str
    count: int


class CertificationRowOut(BaseModel):
    provider: str
    certification_name: str
    completed: int
    pending: int
    on_track: bool


class DashboardOverviewOut(BaseModel):
    data_as_of: datetime
    providers: list[ProviderLeaderboardOut]  # fixed Provider enum order; frontend sorts by completed for display
    practices: list[PracticeMetaOut]  # ordered by headcount desc
    matrix: list[MatrixCellOut]  # only (provider, practice) pairs with count > 0 — frontend fills the rest as 0
    certifications: list[CertificationRowOut]


@router.get("/overview", response_model=DashboardOverviewOut)
def dashboard_overview(db: Session = Depends(get_portal_db)):
    completed_by_provider = dict(
        db.execute(
            select(CertificationRecord.provider, func.count(func.distinct(_IDENTITY)))
            .where(CertificationRecord.status_canonical == StatusCanonical.completed)
            .group_by(CertificationRecord.provider)
        ).all()
    )
    targets = dict(db.execute(select(ProviderTarget.provider, ProviderTarget.target_count)).all())
    providers_out = [
        ProviderLeaderboardOut(provider=p.value, completed=completed_by_provider.get(p, 0), target=targets.get(p))
        for p in Provider
    ]

    # Practice "headcount" is a real, disclosed proxy, not the org's true HR
    # roster: this dataset only ever tracks people who attempted/completed at
    # least one certification, so headcount here means "distinct people on
    # file in this practice across any provider/status" — the denominator
    # the Section-3c rings use is honestly this, not a headcount pulled from
    # an independent employee system (no such roster exists in this data —
    # the main app's own `employees` table was cleared 2026-09-09 and was
    # never this dataset's source anyway).
    headcount_map = dict(
        db.execute(
            select(CertificationRecord.practice_canonical, func.count(func.distinct(_IDENTITY)))
            .where(CertificationRecord.practice_canonical.is_not(None))
            .group_by(CertificationRecord.practice_canonical)
        ).all()
    )
    # Most common PD per practice (ties -> alphabetically first, matching
    # Postgres' mode()). Computed here because Databricks has no
    # mode() WITHIN GROUP aggregate.
    pd_map: dict[str, str] = {}
    for practice, pd_name, n in sorted(
        db.execute(
            select(CertificationRecord.practice_canonical, CertificationRecord.pd_leader_name, func.count())
            .where(
                CertificationRecord.practice_canonical.is_not(None),
                CertificationRecord.pd_leader_name.is_not(None),
            )
            .group_by(CertificationRecord.practice_canonical, CertificationRecord.pd_leader_name)
        ).all(),
        key=lambda r: (r[0], -r[2], r[1]),
    ):
        pd_map.setdefault(practice, pd_name)
    practices_out = [
        PracticeMetaOut(practice=practice, pd_leader_name=pd_map.get(practice), headcount=headcount)
        for practice, headcount in sorted(headcount_map.items(), key=lambda kv: kv[1], reverse=True)
    ]

    matrix_rows = db.execute(
        select(
            CertificationRecord.provider,
            CertificationRecord.practice_canonical,
            func.count(func.distinct(_IDENTITY)),
        )
        .where(
            CertificationRecord.status_canonical == StatusCanonical.completed,
            CertificationRecord.practice_canonical.is_not(None),
        )
        .group_by(CertificationRecord.provider, CertificationRecord.practice_canonical)
    ).all()
    matrix_out = [MatrixCellOut(provider=provider.value, practice=practice, count=count) for provider, practice, count in matrix_rows]

    cert_rows = db.execute(
        select(
            CertificationRecord.provider,
            CertificationRecord.certification_name,
            CertificationRecord.status_canonical,
            func.count(func.distinct(_IDENTITY)),
        )
        .where(CertificationRecord.certification_name.is_not(None))
        .group_by(CertificationRecord.provider, CertificationRecord.certification_name, CertificationRecord.status_canonical)
    ).all()
    cert_map: dict[tuple[Provider, str], dict[StatusCanonical, int]] = {}
    for provider, name, status, count in cert_rows:
        cert_map.setdefault((provider, name), {})[status] = count

    provider_order = {p: i for i, p in enumerate(Provider)}
    certifications_out = []
    for (provider, name), counts in cert_map.items():
        completed = counts.get(StatusCanonical.completed, 0)
        pending = counts.get(StatusCanonical.in_progress, 0)
        if not completed and not pending:
            continue  # nothing to show here (cert only has not_cleared/unknown rows) — not an empty/misleading 0/0 row
        denom = completed + pending
        certifications_out.append(
            CertificationRowOut(
                provider=provider.value,
                certification_name=name,
                completed=completed,
                pending=pending,
                on_track=(completed / denom) >= _ON_TRACK_THRESHOLD,
            )
        )
    certifications_out.sort(key=lambda c: (provider_order[Provider(c.provider)], -c.completed))

    return DashboardOverviewOut(
        data_as_of=datetime.now(timezone.utc),
        providers=providers_out,
        practices=practices_out,
        matrix=matrix_out,
        certifications=certifications_out,
    )
