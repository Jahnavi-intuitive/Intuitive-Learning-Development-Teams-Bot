"""Next-certification recommender API (services/recommender/). Replaces
routers/learning_paths.py's /next and /overview, which only walked one
practice's hand-drawn prerequisite path and ignored experience, partner
needs and everything outside that one path.

Employee-facing endpoints are self-only, same rule the old /next had:
`employee_id` must be the authenticated caller's own (get_current_employee).

Admin endpoints edit the partner catalog and partnership requirements in
the *portal* DB, auditing into the main DB's audit_log — the same two-
database write pattern as routers/portal_admin.py (portal commit first,
then the audit entry; see that module's docstring for why).
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Employee, EmployeeSkillProfile, RecommendationFeedback
from app.portal_db import get_portal_db
from app.portal_models import PartnerCertification, PartnershipRequirement, Provider
from app.schemas import (
    DomainProficiencyOut,
    HeldCertOut,
    LearnerProfileOut,
    NextCertificationsOut,
    PartnerCertificationIn,
    PartnerCertificationOut,
    PartnershipGapOut,
    PartnershipRequirementIn,
    PartnershipRequirementOut,
    RecommendationFeedbackIn,
    RecommendationOut,
    SkillProfileIn,
    SkippableOut,
)
from app.services.access_scope import get_current_employee
from app.services.admin_auth import require_admin
from app.services.audit import serialize_row, write_audit_entry
from app.services.audited_crud import create_with_audit, update_with_audit
from app.services.recommender import core, engine
from app.services.recommender.taxonomy import DOMAINS, LEVELS, RANK_LABELS, cert_key, parse_proficiency

router = APIRouter(tags=["recommendations"])


def _self_only(employee_id: str, current: Employee) -> Employee:
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only access your own recommendations/profile.")
    return current


def _profile_out(p: core.LearnerProfile) -> LearnerProfileOut:
    return LearnerProfileOut(
        employee_id=p.employee_id,
        practice=p.practice,
        role=p.role,
        seniority=p.seniority,
        seniority_basis=p.seniority_basis,
        years_experience=p.years_experience,
        proficiency=[
            DomainProficiencyOut(
                domain=d.domain, domain_label=DOMAINS.get(d.domain, d.domain),
                level=d.level, level_label=RANK_LABELS[d.level], basis=d.basis,
            )
            for d in sorted(p.proficiency.values(), key=lambda d: (-d.level, d.domain))
        ],
        career_goals=p.career_goals,
        completed=[HeldCertOut(provider=h.provider, certification_name=h.name, status=h.status, source=h.source)
                   for h in p.completed],
        in_progress=[HeldCertOut(provider=h.provider, certification_name=h.name, status=h.status, source=h.source)
                     for h in p.in_progress],
        missing_signals=p.missing_signals,
    )


# --- Employee-facing -----------------------------------------------------------
@router.get("/api/recommendations/next-certifications", response_model=NextCertificationsOut)
def next_certifications(
    employee_id: str,
    top_n: int = Query(default=5, ge=1, le=15),
    focused: bool = Query(default=True, description="One recommendation (two only on a same-priority tie)"),
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    current: Employee = Depends(get_current_employee),
):
    employee = _self_only(employee_id, current)
    out = engine.recommend_for(db, portal_db, employee, top_n=top_n, focused=focused)
    r = out.result
    return NextCertificationsOut(
        profile=_profile_out(r.profile),
        recommendations=[RecommendationOut(**rec.__dict__) for rec in r.recommendations],
        skippable=[SkippableOut(**s.__dict__) for s in r.skippable],
        catalog_size=r.catalog_size,
        catalog_source=r.catalog_source,
        signals_unavailable=out.signals_unavailable,
        data_as_of=out.data_as_of,
    )


@router.get("/api/recommendations/profile", response_model=LearnerProfileOut)
def get_learner_profile(
    employee_id: str,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    current: Employee = Depends(get_current_employee),
):
    employee = _self_only(employee_id, current)
    snap = engine.load_portal_snapshot(portal_db)
    return _profile_out(engine.build_learner_profile(db, employee, snap, datetime.now(timezone.utc).date()))


@router.put("/api/recommendations/profile", response_model=LearnerProfileOut)
def update_skill_profile(
    payload: SkillProfileIn,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    current: Employee = Depends(get_current_employee),
):
    employee = _self_only(payload.employee_id, current)
    changes: dict = {}
    if payload.years_experience is not None:
        changes["years_experience"] = payload.years_experience
    if payload.expertise is not None:
        expertise = {}
        for domain, value in payload.expertise.items():
            if domain not in DOMAINS:
                raise HTTPException(status_code=422, detail=f"Unknown skill domain '{domain}'. Valid: {sorted(DOMAINS)}")
            try:
                expertise[domain] = parse_proficiency(value)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{domain}: {exc}") from exc
        changes["expertise"] = expertise
    if payload.career_goals is not None:
        unknown = [g for g in payload.career_goals if g not in DOMAINS]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown skill domain(s) {unknown}. Valid: {sorted(DOMAINS)}")
        changes["career_goals"] = list(dict.fromkeys(payload.career_goals))

    existing = engine.get_skill_profile(db, employee)
    if existing is None:
        create_with_audit(db, EmployeeSkillProfile, actor=employee.employee_id, employee_id=employee.id,
                          source="self_declared", **{"expertise": {}, "career_goals": [], **changes})
    elif changes:
        if "expertise" in changes:
            # Merge, so declaring one new domain doesn't wipe the others.
            changes["expertise"] = {**(existing.expertise or {}), **changes["expertise"]}
        update_with_audit(db, existing, actor=employee.employee_id, changes=changes)
    db.commit()

    snap = engine.load_portal_snapshot(portal_db)
    return _profile_out(engine.build_learner_profile(db, employee, snap, datetime.now(timezone.utc).date()))


@router.post("/api/recommendations/feedback", status_code=201)
def record_feedback(
    payload: RecommendationFeedbackIn,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    employee = _self_only(payload.employee_id, current)
    if payload.action not in ("dismissed", "interested"):
        raise HTTPException(status_code=422, detail="action must be 'dismissed' or 'interested'.")
    create_with_audit(
        db, RecommendationFeedback, actor=employee.employee_id, employee_id=employee.id,
        provider=payload.provider, certification_name=payload.certification_name,
        action=payload.action, reason=payload.reason,
    )
    db.commit()
    return {"recorded": True, "suppressed_for_days": engine.DISMISS_TTL_DAYS if payload.action == "dismissed" else 0}


@router.get("/api/recommendations/taxonomy")
def taxonomy():
    """Valid skill-domain keys and level names — what the agent maps
    free-text answers ("5 years on AWS") onto before calling PUT /profile."""
    return {
        "domains": [{"key": k, "label": v} for k, v in DOMAINS.items()],
        "levels": [{"name": k, "rank": v} for k, v in LEVELS.items()],
        "proficiency_scale": RANK_LABELS,
    }


@router.get("/api/portal/catalog", response_model=list[PartnerCertificationOut])
def portal_catalog(q: str | None = None, portal_db: Session = Depends(get_portal_db)):
    """Read-only list of every active certification in the partner catalog,
    for the portal's Certification Catalog tab and the agent's catalog
    lookup (no admin gate: it's the same reference data the recommender
    chooses from, not employee data). `q` keeps rows whose name contains
    every word of it (provider name included), in any order — "aws
    architect" finds both AWS Solutions Architect certifications."""
    rows = portal_db.scalars(
        select(PartnerCertification)
        .where(PartnerCertification.is_active.is_(True))
        .order_by(PartnerCertification.provider, PartnerCertification.certification_name)
    ).all()
    if q:
        words = [w for w in cert_key(None, q)[1].split() if w]
        rows = [r for r in rows if all(w in cert_key(r.provider.value, r.certification_name)[1] + " " + r.provider.value
                                      for w in words)]
    return [_cert_out(r) for r in rows]


@router.get("/api/recommendations/partnership-gaps", response_model=list[PartnershipGapOut])
def partnership_gaps(portal_db: Session = Depends(get_portal_db)):
    """Every active partnership requirement with its live certified count
    and gap, largest gap first. Aggregate counts only (no names) — open,
    like the portal Dashboard (routers/portal_dashboard.py)."""
    reqs = engine.compute_requirements(engine.load_portal_snapshot(portal_db))
    reqs.sort(key=lambda r: (-r.gap, -r.priority, r.provider))
    return [
        PartnershipGapOut(
            requirement_id=r.requirement_id, provider=r.provider, program_name=r.program_name,
            certification_name=r.certification_name, min_level=r.min_level, skill_domain=r.domain,
            practice=r.practice, required_count=r.required_count, current_count=r.current_count,
            gap=r.gap, deadline=r.deadline, priority=r.priority,
        )
        for r in reqs
    ]


# --- Admin: partner catalog & partnership requirements (portal DB) -------------
def _provider(value: str) -> Provider:
    try:
        return Provider(value.lower())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown partner '{value}'. Valid: {[p.value for p in Provider]}") from exc


def _check_level(level: str | None) -> None:
    if level is not None and level not in LEVELS:
        raise HTTPException(status_code=422, detail=f"Unknown level '{level}'. Valid: {list(LEVELS)}")


def _check_domains(domains: list[str]) -> None:
    unknown = [d for d in domains if d not in DOMAINS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown skill domain(s) {unknown}. Valid: {sorted(DOMAINS)}")


def _audit(db: Session, entity_type: str, row_id: int, action: str, actor: str, old, new) -> None:
    write_audit_entry(
        db, entity_type=entity_type, entity_id=uuid.uuid5(uuid.NAMESPACE_OID, f"{entity_type}:{row_id}"),
        action=action, actor=actor, old_value=old, new_value=new,
    )
    db.commit()


def _cert_out(row: PartnerCertification) -> PartnerCertificationOut:
    return PartnerCertificationOut(
        id=row.id, provider=row.provider.value, certification_name=row.certification_name, level=row.level,
        skill_domains=row.skill_domains or [], relevant_practices=row.relevant_practices or [],
        prerequisite_names=row.prerequisite_names or [], validity_months=row.validity_months, is_active=row.is_active,
    )


def _req_out(row: PartnershipRequirement) -> PartnershipRequirementOut:
    return PartnershipRequirementOut(
        id=row.id, provider=row.provider.value, program_name=row.program_name, required_count=row.required_count,
        certification_name=row.certification_name, min_level=row.min_level, skill_domain=row.skill_domain,
        practice=row.practice, deadline=row.deadline, priority=row.priority, notes=row.notes, is_active=row.is_active,
    )


@router.get("/api/admin/partner-catalog", response_model=list[PartnerCertificationOut])
def list_partner_catalog(portal_db: Session = Depends(get_portal_db), admin: Employee = Depends(require_admin)):
    rows = portal_db.scalars(select(PartnerCertification).order_by(PartnerCertification.provider,
                                                                   PartnerCertification.certification_name)).all()
    return [_cert_out(r) for r in rows]


@router.put("/api/admin/partner-catalog", response_model=PartnerCertificationOut)
def upsert_partner_certification(
    payload: PartnerCertificationIn,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    """Upsert keyed on (partner, certification_name)."""
    provider = _provider(payload.provider)
    _check_level(payload.level)
    _check_domains(payload.skill_domains)
    row = portal_db.scalar(select(PartnerCertification).where(
        PartnerCertification.provider == provider,
        PartnerCertification.certification_name == payload.certification_name,
    ))
    old = serialize_row(row) if row else None
    fields = payload.model_dump(exclude={"provider"})
    if row is None:
        row = PartnerCertification(provider=provider, updated_by=admin.employee_id, **fields)
        portal_db.add(row)
    else:
        for k, v in fields.items():
            setattr(row, k, v)
        row.updated_by = admin.employee_id
    portal_db.commit()
    portal_db.refresh(row)
    _audit(db, "partner_certification", row.id, "created" if old is None else "field_updated",
           admin.employee_id, old, serialize_row(row))
    return _cert_out(row)


@router.get("/api/admin/partnership-requirements", response_model=list[PartnershipRequirementOut])
def list_requirements(portal_db: Session = Depends(get_portal_db), admin: Employee = Depends(require_admin)):
    rows = portal_db.scalars(select(PartnershipRequirement).order_by(PartnershipRequirement.provider,
                                                                     PartnershipRequirement.id)).all()
    return [_req_out(r) for r in rows]


@router.post("/api/admin/partnership-requirements", response_model=PartnershipRequirementOut, status_code=201)
def create_requirement(
    payload: PartnershipRequirementIn,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    provider = _provider(payload.provider)
    _check_level(payload.min_level)
    if payload.skill_domain:
        _check_domains([payload.skill_domain])
    row = PartnershipRequirement(provider=provider, updated_by=admin.employee_id,
                                 **payload.model_dump(exclude={"provider"}))
    portal_db.add(row)
    portal_db.commit()
    portal_db.refresh(row)
    _audit(db, "partnership_requirement", row.id, "created", admin.employee_id, None, serialize_row(row))
    return _req_out(row)


@router.put("/api/admin/partnership-requirements/{requirement_id}", response_model=PartnershipRequirementOut)
def update_requirement(
    requirement_id: int,
    payload: PartnershipRequirementIn,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    row = portal_db.get(PartnershipRequirement, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Partnership requirement not found.")
    provider = _provider(payload.provider)
    _check_level(payload.min_level)
    if payload.skill_domain:
        _check_domains([payload.skill_domain])
    old = serialize_row(row)
    row.provider = provider
    for k, v in payload.model_dump(exclude={"provider"}).items():
        setattr(row, k, v)
    row.updated_by = admin.employee_id
    portal_db.commit()
    portal_db.refresh(row)
    _audit(db, "partnership_requirement", row.id, "field_updated", admin.employee_id, old, serialize_row(row))
    return _req_out(row)


@router.delete("/api/admin/partnership-requirements/{requirement_id}", status_code=204)
def delete_requirement(
    requirement_id: int,
    db: Session = Depends(get_db),
    portal_db: Session = Depends(get_portal_db),
    admin: Employee = Depends(require_admin),
):
    row = portal_db.get(PartnershipRequirement, requirement_id)
    if row is None:
        return None
    old = serialize_row(row)
    portal_db.delete(row)
    portal_db.commit()
    _audit(db, "partnership_requirement", requirement_id, "deleted", admin.employee_id, old, None)
    return None
