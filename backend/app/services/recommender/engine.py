"""DB-facing side of the recommender: loads everything core.recommend()
needs from both databases and hands it over as plain dataclasses.

Sources, and which database each comes from:
- partner catalog            portal  partner_certifications (fallback: distinct
                                     certs in certification_records)
- partnership requirements   portal  partnership_requirements + provider_targets,
                                     current counts recomputed live from
                                     certification_records
- peer signal                portal  certification_records (completed)
- the person's own certs     main    certificate_records  ∪  portal
                                     certification_records matched by email,
                                     then by normalized name (the portal's own
                                     identity rule — see portal_dashboard._IDENTITY)
- experience / expertise     main    employee_skill_profiles
- mandatory drives           main    drive_enrollments (open, non-compliant)
- dismissals                 main    recommendation_feedback

The portal database is allowed to be empty (it is, today, before real
data lands) or unreachable: every portal-derived signal then degrades to
"no signal" and the response says so via `signals_unavailable`, rather
than the whole recommendation failing.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    CertificateRecord,
    CertificationCatalog,
    DriveCertification,
    DriveEnrollment,
    Employee,
    EmployeeSkillProfile,
    RecommendationFeedback,
)
from app.portal_models import (
    CertificationRecord,
    PartnerCertification,
    PartnershipRequirement,
    ProviderTarget,
    StatusCanonical,
)
from app.services.recommender import core
from app.services.recommender.taxonomy import cert_key, normalize_level, normalize_practice

log = logging.getLogger(__name__)

DISMISS_TTL_DAYS = 90

_MAIN_STATUS = {"completed": "completed", "in_progress": "in_progress", "gave_exam_but_failed": "failed"}
_PORTAL_STATUS = {
    StatusCanonical.completed: "completed",
    StatusCanonical.in_progress: "in_progress",
    StatusCanonical.not_cleared: "failed",
}


_PARTNER_LABELS = {"aws": "AWS", "gemini": "Google", "anthropic": "Anthropic", "databricks": "Databricks",
                   "azure": "Azure"}


def _identity(name: str | None) -> str:
    return (name or "").strip().lower()


@dataclass
class _PortalSnapshot:
    catalog: list[core.CatalogCert] = field(default_factory=list)
    catalog_source: str = "partner_catalog"
    records: list[CertificationRecord] = field(default_factory=list)
    requirements: list[PartnershipRequirement] = field(default_factory=list)
    targets: list[ProviderTarget] = field(default_factory=list)
    available: bool = True


def _load_portal(portal_db: Session) -> _PortalSnapshot:
    snap = _PortalSnapshot()
    try:
        snap.records = list(portal_db.scalars(select(CertificationRecord)).all())
        rows = portal_db.scalars(select(PartnerCertification).where(PartnerCertification.is_active.is_(True))).all()
        snap.requirements = list(portal_db.scalars(
            select(PartnershipRequirement).where(PartnershipRequirement.is_active.is_(True))
        ).all())
        snap.targets = list(portal_db.scalars(select(ProviderTarget)).all())
    except SQLAlchemyError:
        log.warning("Portal DB unavailable — recommending from the bundled catalog, without gap/peer signals",
                    exc_info=True)
        portal_db.rollback()
        snap.available = False
        snap.catalog, snap.catalog_source = _bundled_catalog(), "bundled_catalog"
        return snap

    if rows:
        snap.catalog = [
            core.CatalogCert(
                provider=r.provider.value,
                name=r.certification_name,
                level=r.level,
                domains=tuple(r.skill_domains or ()),
                practices=tuple(r.relevant_practices or ()),
                prerequisites=tuple(r.prerequisite_names or ()),
                validity_months=r.validity_months,
            )
            for r in rows
        ]
    else:
        # No curated catalog yet — fall back to every certification the
        # portal already tracks (any status), with level/domain inferred.
        snap.catalog_source = "portal_records"
        seen: dict[tuple, core.CatalogCert] = {}
        for r in snap.records:
            c = _record_as_cert(r)
            if c is None:
                continue
            # Prefer a row that actually carries an explicit level.
            if c.key not in seen or (r.certification_level and not seen[c.key].level):
                seen[c.key] = c
        snap.catalog = list(seen.values())
        if not snap.catalog:
            # Nothing synced from the partner portal either — still recommend,
            # from the bundled starting catalog.
            snap.catalog, snap.catalog_source = _bundled_catalog(), "bundled_catalog"
    return snap


_BUNDLED_CATALOG = Path(__file__).resolve().parents[4] / "scripts" / "seed_data" / "partner_catalog.json"


def _bundled_catalog() -> list[core.CatalogCert]:
    """The same starting catalog scripts/seed_partner_catalog.py seeds into
    partner_certifications, read straight from the JSON — the last-resort
    source so a recommendation can always be made from the catalog and the
    person's role/practice, even with no partner data at all."""
    try:
        entries = json.loads(_BUNDLED_CATALOG.read_text(encoding="utf-8"))["certifications"]
    except (OSError, ValueError, KeyError):
        log.warning("Bundled catalog %s unreadable — no catalog to recommend from", _BUNDLED_CATALOG, exc_info=True)
        return []
    return [
        core.CatalogCert(
            provider=e["provider"], name=e["certification_name"], level=e.get("level"),
            domains=tuple(e.get("skill_domains") or ()), practices=tuple(e.get("relevant_practices") or ()),
            prerequisites=tuple(e.get("prerequisite_names") or ()), validity_months=e.get("validity_months"),
            source="bundled_catalog",
        )
        for e in entries
    ]


def _record_as_cert(r: CertificationRecord) -> core.CatalogCert | None:
    if not r.certification_name:
        return None
    return core.CatalogCert(
        provider=r.provider.value, name=r.certification_name,
        level=normalize_level(r.certification_level, r.certification_name), source="portal_records",
    )


def compute_requirements(snap: _PortalSnapshot) -> list[core.Requirement]:
    """Every active partnership requirement (plus each provider target as a
    coarser provider-wide one) with its *live* current certified count:
    distinct people, completed status, matching the requirement's filter."""
    completed = [(r, _record_as_cert(r)) for r in snap.records if r.status_canonical == StatusCanonical.completed]
    completed = [(r, c) for r, c in completed if c is not None]

    out: list[core.Requirement] = []
    for req in snap.requirements:
        shell = core.Requirement(
            requirement_id=f"req-{req.id}", provider=req.provider.value, program_name=req.program_name,
            required_count=req.required_count, current_count=0,
            certification_name=req.certification_name, min_level=req.min_level, domain=req.skill_domain,
            practice=req.practice, deadline=req.deadline, priority=req.priority,
        )
        people = {_identity(r.person_name) for r, c in completed if shell.matches(c, r.practice_canonical)}
        out.append(core.Requirement(**{**shell.__dict__, "current_count": len(people)}))

    for t in snap.targets:
        people = {_identity(r.person_name) for r, c in completed if r.provider == t.provider}
        out.append(core.Requirement(
            requirement_id=f"target-{t.provider.value}", provider=t.provider.value,
            program_name=f"{_PARTNER_LABELS.get(t.provider.value, t.provider.value)} org-wide certified target",
            required_count=t.target_count, current_count=len(people), priority=2,
        ))
    return out


def _peer_stats(snap: _PortalSnapshot, me: str, my_practice: str | None, my_completed: set[str]) -> core.PeerStats:
    holdings: dict[str, set] = defaultdict(set)
    practice_of: dict[str, str | None] = {}
    for r in snap.records:
        if r.status_canonical != StatusCanonical.completed or not r.certification_name:
            continue
        ident = _identity(r.person_name)
        holdings[ident].add(cert_key(r.provider.value, r.certification_name))
        practice_of.setdefault(ident, r.practice_canonical)
    holdings.pop(me, None)

    stats = core.PeerStats()
    target_practice = normalize_practice(my_practice)
    if target_practice:
        cohort = [h for ident, h in holdings.items() if normalize_practice(practice_of.get(ident)) == target_practice]
        stats.practice_population = len(cohort)
        counts: dict[tuple, int] = defaultdict(int)
        for h in cohort:
            for k in h:
                counts[k] += 1
        stats.practice_share = {k: v / len(cohort) for k, v in counts.items()} if cohort else {}
    if my_completed:
        co = [h for h in holdings.values() if {k[1] for k in h} & my_completed]
        stats.co_holder_population = len(co)
        counts = defaultdict(int)
        for h in co:
            for k in h:
                counts[k] += 1
        stats.co_holder_share = {k: v / len(co) for k, v in counts.items()} if co else {}
    return stats


def _held_certs(db: Session, employee: Employee, snap: _PortalSnapshot) -> list[core.HeldCert]:
    held: list[core.HeldCert] = []
    rows = db.execute(
        select(CertificateRecord, CertificationCatalog.level)
        .outerjoin(CertificationCatalog, CertificationCatalog.id == CertificateRecord.certification_id)
        .where(CertificateRecord.employee_id == employee.id)
    ).all()
    for rec, level in rows:
        held.append(core.HeldCert(
            provider=None, name=rec.cert_name, status=_MAIN_STATUS[rec.submission_status],
            level=level, expiry_date=rec.expiration_date, source="tracker",
        ))

    email = (employee.email or "").strip().lower()
    ident = _identity(employee.name)
    for r in snap.records:
        if not r.certification_name or r.status_canonical not in _PORTAL_STATUS:
            continue
        if (email and r.person_email_normalized == email) or _identity(r.person_name) == ident:
            held.append(core.HeldCert(
                provider=r.provider.value, name=r.certification_name, status=_PORTAL_STATUS[r.status_canonical],
                level=r.certification_level, completion_date=r.completion_date, expiry_date=r.expiry_date,
                source="partner_portal",
            ))
    return held


def _drives(db: Session, employee: Employee) -> list[core.DriveObligation]:
    out = []
    enrollments = db.scalars(
        select(DriveEnrollment)
        .where(DriveEnrollment.employee_id == employee.id)
        .options(selectinload(DriveEnrollment.drive), selectinload(DriveEnrollment.certificate_record))
    ).all()
    for e in enrollments:
        if e.drive.status != "active" or e.enrollment_status == "exempted":
            continue
        names = db.scalars(
            select(CertificationCatalog.name)
            .join(DriveCertification, DriveCertification.certification_id == CertificationCatalog.id)
            .where(DriveCertification.drive_id == e.drive.id)
        ).all()
        compliant = e.certificate_record is not None and e.certificate_record.submission_status == "completed"
        out.append(core.DriveObligation(e.drive.name, tuple(names), e.drive.deadline, compliant))
    return out


def _dismissed(db: Session, employee: Employee, now: datetime) -> set:
    rows = db.scalars(
        select(RecommendationFeedback)
        .where(
            RecommendationFeedback.employee_id == employee.id,
            RecommendationFeedback.created_at >= now - timedelta(days=DISMISS_TTL_DAYS),
        )
        .order_by(RecommendationFeedback.created_at)
    ).all()
    latest: dict[tuple, str] = {}
    for r in rows:
        latest[cert_key(r.provider, r.certification_name)] = r.action
    return {k for k, action in latest.items() if action == "dismissed"}


def get_skill_profile(db: Session, employee: Employee) -> EmployeeSkillProfile | None:
    return db.scalar(select(EmployeeSkillProfile).where(EmployeeSkillProfile.employee_id == employee.id))


def load_portal_snapshot(portal_db: Session) -> _PortalSnapshot:
    return _load_portal(portal_db)


def build_learner_profile(
    db: Session, employee: Employee, snap: _PortalSnapshot, today: date
) -> core.LearnerProfile:
    sp = get_skill_profile(db, employee)
    return core.build_profile(
        core.ProfileInput(
            employee_id=employee.employee_id,
            name=employee.name,
            practice=employee.practice,
            role=employee.role,
            years_experience=sp.years_experience if sp else None,
            declared_expertise=dict(sp.expertise) if sp else {},
            career_goals=list(sp.career_goals) if sp else [],
            held=_held_certs(db, employee, snap),
        ),
        today,
    )


@dataclass
class EngineResult:
    result: core.RecommendationResult
    signals_unavailable: list[str]
    data_as_of: datetime


def recommend_for(
    db: Session, portal_db: Session, employee: Employee, top_n: int = 5, focused: bool = True
) -> EngineResult:
    """focused=True (the default, what employees see): one recommendation,
    or two only when tied at the same priority — see
    core.focus_recommendations. focused=False returns the full top_n list."""
    now = datetime.now(timezone.utc)
    today = now.date()
    snap = _load_portal(portal_db)
    profile = build_learner_profile(db, employee, snap, today)
    requirements = compute_requirements(snap)
    peers = _peer_stats(snap, _identity(employee.name), employee.practice, profile.completed_keys)

    unavailable = []
    if not snap.available:
        unavailable.append("partner_portal")
    else:
        if not snap.catalog:
            unavailable.append("partner_catalog")
        if not requirements:
            unavailable.append("partnership_requirements")
        if not snap.records:
            unavailable.append("peer_signal")

    result = core.recommend(
        profile=profile,
        catalog=snap.catalog,
        requirements=requirements,
        drives=_drives(db, employee),
        peers=peers,
        dismissed=_dismissed(db, employee, now),
        today=today,
        top_n=top_n,
        catalog_source=snap.catalog_source,
    )
    if focused:
        result.recommendations = core.focus_recommendations(result.recommendations)
    return EngineResult(result=result, signals_unavailable=unavailable, data_as_of=now)
