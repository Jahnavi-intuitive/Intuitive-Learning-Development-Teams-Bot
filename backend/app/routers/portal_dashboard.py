"""KPI/insight endpoints for the new L&D portal Dashboard tab (frontend/portal.html)
— reads exclusively from the lnd_portal database (app/portal_db.py,
app/portal_models.py), the real Anthropic/Gemini/AWS/Databricks certification
data ingested by scripts/ingest_real_excel_data.py. Deliberately open, no
auth gate: matches routers/dashboards.py's leadership-dashboard precedent
(a live, no-login read-only screen), and this router only ever reads.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.portal_db import get_portal_db
from app.portal_models import CertificationRecord, Provider, StatusCanonical

router = APIRouter(prefix="/api/portal/dashboard", tags=["portal-dashboard"])

_STATUS_ORDER = [StatusCanonical.completed, StatusCanonical.in_progress, StatusCanonical.not_cleared, StatusCanonical.unknown]


class TotalsOut(BaseModel):
    total_records: int
    completed: int
    in_progress: int
    not_cleared: int
    unknown: int
    distinct_certifications: int
    providers_covered: int


class StatusCountOut(BaseModel):
    status: str
    count: int


class ProviderStatusOut(BaseModel):
    provider: str
    completed: int
    in_progress: int
    not_cleared: int
    unknown: int
    total: int
    distinct_certifications: int


class CertificationBreakdownOut(BaseModel):
    certification_name: str
    provider: str
    completed: int
    in_progress: int
    not_cleared: int
    unknown: int
    total: int


class ProviderCertGroupOut(BaseModel):
    provider: str
    certifications: list[CertificationBreakdownOut]


class PracticeCountOut(BaseModel):
    practice: str
    total_count: int
    completed_count: int


class DashboardSummaryOut(BaseModel):
    data_as_of: datetime
    totals: TotalsOut
    by_status: list[StatusCountOut]
    by_provider: list[ProviderStatusOut]
    by_certification: list[ProviderCertGroupOut]
    by_practice: list[PracticeCountOut]


@router.get("/summary", response_model=DashboardSummaryOut)
def dashboard_summary(db: Session = Depends(get_portal_db)):
    status_counts = dict(
        db.execute(
            select(CertificationRecord.status_canonical, func.count()).group_by(CertificationRecord.status_canonical)
        ).all()
    )
    total_records = sum(status_counts.values())
    distinct_certifications = db.scalar(
        select(func.count(func.distinct(CertificationRecord.certification_name))).where(
            CertificationRecord.certification_name.is_not(None)
        )
    )
    providers_covered = db.scalar(select(func.count(func.distinct(CertificationRecord.provider))))

    totals = TotalsOut(
        total_records=total_records,
        completed=status_counts.get(StatusCanonical.completed, 0),
        in_progress=status_counts.get(StatusCanonical.in_progress, 0),
        not_cleared=status_counts.get(StatusCanonical.not_cleared, 0),
        unknown=status_counts.get(StatusCanonical.unknown, 0),
        distinct_certifications=distinct_certifications or 0,
        providers_covered=providers_covered or 0,
    )

    by_status = [
        StatusCountOut(status=s.value, count=status_counts.get(s, 0))
        for s in _STATUS_ORDER
        if status_counts.get(s, 0) > 0
    ]

    # Full per-provider status breakdown for the KPI row — per direct
    # instruction, completed/in-progress/not-cleared/completion-rate are
    # never shown as one combined org-wide number; each provider gets its
    # own figures. Computed here (not derived from the possibly-capped
    # by_certification list below) so it's exact even for a provider with
    # more distinct certs than that list's per-provider cap keeps. Fixed
    # declaration order (not sorted by count) so KPI cards don't reshuffle
    # position as data changes.
    provider_status_rows = db.execute(
        select(CertificationRecord.provider, CertificationRecord.status_canonical, func.count())
        .group_by(CertificationRecord.provider, CertificationRecord.status_canonical)
    ).all()
    provider_status_map: dict[Provider, dict[StatusCanonical, int]] = {}
    for provider, status, count in provider_status_rows:
        provider_status_map.setdefault(provider, {})[status] = count

    provider_distinct_certs = dict(
        db.execute(
            select(CertificationRecord.provider, func.count(func.distinct(CertificationRecord.certification_name)))
            .where(CertificationRecord.certification_name.is_not(None))
            .group_by(CertificationRecord.provider)
        ).all()
    )
    by_provider = [
        ProviderStatusOut(
            provider=p.value,
            completed=provider_status_map.get(p, {}).get(StatusCanonical.completed, 0),
            in_progress=provider_status_map.get(p, {}).get(StatusCanonical.in_progress, 0),
            not_cleared=provider_status_map.get(p, {}).get(StatusCanonical.not_cleared, 0),
            unknown=provider_status_map.get(p, {}).get(StatusCanonical.unknown, 0),
            total=sum(provider_status_map.get(p, {}).values()),
            distinct_certifications=provider_distinct_certs.get(p, 0),
        )
        for p in Provider
        if p in provider_status_map
    ]

    # Per-certification status breakdown, grouped strictly by provider — per
    # direct instruction, certifications from different providers are never
    # ranked/interleaved together in one combined list. Each provider's own
    # certifications are sorted by total within that provider only, and
    # capped per provider (not globally) so a provider with many distinct
    # certs (AWS, Databricks) can't crowd out one with few (Anthropic has
    # only 1; Gemini has 3) — every provider's section always appears.
    cert_status_rows = db.execute(
        select(
            CertificationRecord.certification_name, CertificationRecord.provider,
            CertificationRecord.status_canonical, func.count(),
        )
        .where(CertificationRecord.certification_name.is_not(None))
        .group_by(CertificationRecord.certification_name, CertificationRecord.provider, CertificationRecord.status_canonical)
    ).all()
    cert_map: dict[tuple[str, Provider], dict[StatusCanonical, int]] = {}
    for name, provider, status, count in cert_status_rows:
        cert_map.setdefault((name, provider), {})[status] = count

    certs_by_provider: dict[Provider, list[CertificationBreakdownOut]] = {}
    for (name, provider), counts in cert_map.items():
        certs_by_provider.setdefault(provider, []).append(
            CertificationBreakdownOut(
                certification_name=name,
                provider=provider.value,
                completed=counts.get(StatusCanonical.completed, 0),
                in_progress=counts.get(StatusCanonical.in_progress, 0),
                not_cleared=counts.get(StatusCanonical.not_cleared, 0),
                unknown=counts.get(StatusCanonical.unknown, 0),
                total=sum(counts.values()),
            )
        )
    by_certification = [
        ProviderCertGroupOut(
            provider=provider.value,
            certifications=sorted(certs_by_provider[provider], key=lambda c: c.total, reverse=True)[:8],
        )
        for provider in Provider
        if provider in certs_by_provider
    ]

    # Practice is only collected for Anthropic/Gemini/Databricks (AWS's form
    # never captured it — see scripts/ingest_real_excel_data.py) — null
    # practice_canonical rows are excluded here rather than shown as a
    # misleading "Unassigned" bucket that's really just "not collected".
    practice_rows = db.execute(
        select(
            CertificationRecord.practice_canonical,
            func.count(),
            func.count().filter(CertificationRecord.status_canonical == StatusCanonical.completed),
        )
        .where(CertificationRecord.practice_canonical.is_not(None))
        .group_by(CertificationRecord.practice_canonical)
        .order_by(func.count().desc())
        .limit(8)
    ).all()
    by_practice = [
        PracticeCountOut(practice=practice, total_count=total, completed_count=completed)
        for practice, total, completed in practice_rows
    ]

    return DashboardSummaryOut(
        data_as_of=datetime.now(timezone.utc),
        totals=totals,
        by_status=by_status,
        by_provider=by_provider,
        by_certification=by_certification,
        by_practice=by_practice,
    )
