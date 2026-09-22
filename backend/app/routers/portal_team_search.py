# TODO(access-control): This tab surfaces a single named individual's full
# certification history — the most personally-identifiable view in this
# whole portal (more so than Data Download's bulk export, since it's
# specifically framed around looking up one named person). There is NO
# role-based access control on this tab yet. Acceptable only for the
# current internal test group; MUST be restricted to leadership/L&D admin
# roles before wider rollout. Exact "leadership" definition was deliberately
# left open per direct instruction (not needed to build this UI, but must
# be confirmed before RBAC is actually built) — see DEVELOPMENT_LOG.md's
# 2026-09-17 entry. Don't ship this open beyond the test group.
#
# Also note: per explicit instruction this pass, lookups are NOT written to
# audit_log — the original brief for this tab called for that, but the
# user explicitly said not to build it this time. Flagged here and in
# DEVELOPMENT_LOG.md as a deliberate, confirmed deviation from that
# original ask, not an oversight.
"""Find My Team tab (frontend/portal.html) — search real employees by name
and view their full real certification history across all 4 providers.
Reads through the same lnd_portal `certification_records` table the
Dashboard/Trends/Data Download tabs already use — no separate calculation
path. Row-level real data only, unfiltered by status (confirmed with the
user) — the Trends tab's historical-backfill exception does not apply
here, since this is a real current record, not a chart being demoed.

Identity here is the same normalized-name convention already used
everywhere else in this portal (app/routers/portal_dashboard.py's
`_IDENTITY`) — not name+practice or name+email, since AWS never captures
practice, and using a different identity definition just for this one tab
would make its "how many certs does this person have" answer disagree with
what the Dashboard already counts for the same person.
"""

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.portal_db import get_portal_db
from app.portal_models import CertificationRecord, Provider, StatusCanonical

router = APIRouter(prefix="/api/portal/team-search", tags=["portal-team-search"])

_MIN_QUERY_LEN = 2
_MAX_RESULTS = 20
_EXPIRY_WARNING_DAYS = 30  # confirmed with the user — only ever relevant for Anthropic/Gemini rows

_PROVIDER_LABELS = {
    Provider.aws: "AWS", Provider.anthropic: "Anthropic", Provider.azure: "Azure",
    Provider.databricks: "Databricks", Provider.gemini: "Google", Provider.snowflake: "Snowflake",
}
_STATUS_LABELS = {
    StatusCanonical.completed: "Completed",
    StatusCanonical.in_progress: "In Progress",
    StatusCanonical.not_cleared: "Attempted - Not Cleared",
    StatusCanonical.unknown: "Unknown",
}

_IDENTITY = func.lower(func.trim(CertificationRecord.person_name))


class SearchResultOut(BaseModel):
    identity: str
    display_name: str
    email: str | None
    practice: str | None
    pd_leader_name: str | None


@router.get("/search", response_model=list[SearchResultOut])
def search_people(q: str = Query(..., min_length=1), db: Session = Depends(get_portal_db)):
    """Server-side, case-insensitive partial match against real person
    names in certification_records — there's no separate employee roster
    in this schema to search instead (see this module's docstring). Never
    returns the whole roster: a query shorter than 2 characters returns
    nothing, and results are capped."""
    query = q.strip()
    if len(query) < _MIN_QUERY_LEN:
        return []
    rows = db.execute(
        select(
            _IDENTITY,
            func.max(CertificationRecord.person_name),
            func.max(CertificationRecord.person_email),
            func.max(CertificationRecord.practice_canonical),
            func.max(CertificationRecord.pd_leader_name),
        )
        .where(func.lower(CertificationRecord.person_name).contains(query.lower()))
        .group_by(_IDENTITY)
        .order_by(func.max(CertificationRecord.person_name))
        .limit(_MAX_RESULTS)
    ).all()
    return [
        SearchResultOut(identity=identity, display_name=name, email=email, practice=practice, pd_leader_name=pd)
        for identity, name, email, practice, pd in rows
    ]


class CertificationEntryOut(BaseModel):
    provider: str
    provider_label: str
    certification_name: str | None
    status: str
    status_key: str
    completion_date: dt.date | None
    submission_date: dt.date | None
    expiry_date: dt.date | None
    certificate_number: str | None
    expiry_state: str | None  # "expiring_soon" | "expired" | None


class PersonReportOut(BaseModel):
    display_name: str
    email: str | None
    practice: str | None
    pd_leader_name: str | None
    certifications: list[CertificationEntryOut]


@router.get("/person", response_model=PersonReportOut)
def get_person_report(identity: str = Query(..., min_length=1), db: Session = Depends(get_portal_db)):
    """Every real certification_records row for this person, across all
    providers, unfiltered by status (confirmed) — a real current record,
    never backfilled/demo data (that exception is Trends-only)."""
    norm = identity.strip().lower()
    rows = db.execute(select(CertificationRecord).where(_IDENTITY == norm)).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No employee found matching {identity!r}.")

    display_name = next((r.person_name for r in rows if r.person_name), identity)
    email = next((r.person_email for r in rows if r.person_email), None)
    practice = next((r.practice_canonical for r in rows if r.practice_canonical), None)
    pd_leader = next((r.pd_leader_name for r in rows if r.pd_leader_name), None)

    today = dt.date.today()
    certs: list[CertificationEntryOut] = []
    for r in sorted(rows, key=lambda r: (r.provider.value, r.certification_name or "")):
        expiry_state = None
        if r.expiry_date is not None:
            days_left = (r.expiry_date - today).days
            if days_left < 0:
                expiry_state = "expired"
            elif days_left <= _EXPIRY_WARNING_DAYS:
                expiry_state = "expiring_soon"
        certs.append(CertificationEntryOut(
            provider=r.provider.value,
            provider_label=_PROVIDER_LABELS.get(r.provider, r.provider.value),
            certification_name=r.certification_name,
            status=_STATUS_LABELS.get(r.status_canonical, r.status_canonical.value),
            status_key=r.status_canonical.value,
            completion_date=r.completion_date,
            submission_date=r.submission_date,
            expiry_date=r.expiry_date,
            certificate_number=r.certificate_number,
            expiry_state=expiry_state,
        ))

    return PersonReportOut(display_name=display_name, email=email, practice=practice, pd_leader_name=pd_leader, certifications=certs)
