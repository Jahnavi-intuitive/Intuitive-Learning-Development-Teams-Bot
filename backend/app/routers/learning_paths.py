from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import (
    CertificateRecord,
    CertificationCatalog,
    CertificationPrerequisite,
    Employee,
    LearningPath,
    LearningPathItem,
)
from app.schemas import (
    BulkAddToPathsIn,
    LearningPathNextOut,
    LearningPathOut,
    LearningPathOverviewOut,
    NextCertificationCandidate,
    PathCertificationStatusOut,
    PathMembershipIn,
    PathPrerequisiteEdgeOut,
)
from app.services.access_scope import get_current_employee
from app.services.admin_auth import require_admin
from app.services.audited_crud import create_with_audit

router = APIRouter(prefix="/api/learning-paths", tags=["learning_paths"])


def _get_path(db: Session, practice: str) -> LearningPath | None:
    return db.scalar(
        select(LearningPath)
        .where(LearningPath.practice == practice)
        .options(selectinload(LearningPath.items))
    )


@router.get("/next", response_model=LearningPathNextOut)
def next_certification(
    employee_id: str,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    """Role-aware 'next certification(s)' — a tool endpoint the agent calls
    for BR-A-02 (catalog/learning-path questions).

    practice is intentionally NOT a parameter: it's derived from the
    employee's own record, server-side, the same way the submission form
    auto-populates it rather than asking (see 2026-08-24 dev log entry).

    Self-only: `employee_id` must be the authenticated caller's own — this
    is "what should I do next," never "what should someone else do next."
    A real identity check was missing here until the 2026-08-27 security
    pass (see DEVELOPMENT_LOG.md) — employee_id was a plain, unverified
    query param with no get_current_employee dependency at all.

    This is a frontier query over the certification prerequisite DAG, not a
    walk down a single ordered list: it returns every certification in the
    employee's practice path whose prerequisites are already satisfied and
    that the employee hasn't completed yet. That can be zero (path fully
    completed, or none configured), one (a genuine single next step), or
    more than one (a real branch point) — the caller must not assume it's
    always exactly one.
    """
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only query your own next certifications.")
    employee = current
    now = datetime.now(timezone.utc)

    practice = employee.practice
    if practice is None:
        return LearningPathNextOut(
            practice=None, path_name=None, next_certifications=[], data_as_of=now, practice_missing=True
        )

    path = _get_path(db, practice)
    if not path or not path.items:
        return LearningPathNextOut(
            practice=practice, path_name=path.name if path else None, next_certifications=[], data_as_of=now
        )

    completed_cert_ids = {
        r.certification_id
        for r in db.scalars(
            select(CertificateRecord).where(
                CertificateRecord.employee_id == employee.id,
                CertificateRecord.submission_status == "completed",
            )
        )
        if r.certification_id
    }

    path_cert_ids = {item.certification_id for item in path.items}
    not_yet_completed = [item for item in path.items if item.certification_id not in completed_cert_ids]
    if not not_yet_completed:
        return LearningPathNextOut(
            practice=practice, path_name=path.name, next_certifications=[], data_as_of=now
        )

    # Load every prerequisite edge touching this path's certifications in
    # one query, then evaluate each not-yet-completed cert's eligibility
    # in Python — the path sizes here (dozens of certs at most) make a
    # per-candidate query unnecessary.
    edges = db.scalars(
        select(CertificationPrerequisite).where(CertificationPrerequisite.certification_id.in_(path_cert_ids))
    ).all()
    edges_by_cert: dict = {}
    for edge in edges:
        edges_by_cert.setdefault(edge.certification_id, []).append(edge)

    candidates: list[NextCertificationCandidate] = []
    for item in not_yet_completed:
        incoming = edges_by_cert.get(item.certification_id, [])
        if not incoming:
            eligible = True
            reason = "No prerequisites — available from the start of this path."
        else:
            satisfied_flags = [e.prerequisite_certification_id in completed_cert_ids for e in incoming]
            logic = item.certification.prerequisite_logic
            eligible = all(satisfied_flags) if logic == "all" else any(satisfied_flags)
            if eligible:
                satisfied_names = [
                    e.prerequisite.name for e in incoming if e.prerequisite_certification_id in completed_cert_ids
                ]
                reason = f"Prerequisite(s) satisfied ({logic}): {', '.join(satisfied_names)}."

        if eligible:
            # track_label is presentation metadata on the edge, not the
            # certification — use the first edge's label that's actually
            # relevant (satisfied, if any; otherwise the first defined).
            track_label = None
            for e in incoming:
                if e.track_label:
                    track_label = e.track_label
                    break
            candidates.append(
                NextCertificationCandidate(certification=item.certification, track_label=track_label, reason=reason)
            )

    return LearningPathNextOut(
        practice=practice, path_name=path.name, next_certifications=candidates, data_as_of=now
    )


# Registered before /{practice}, same reason as /next above.
@router.get("/overview", response_model=LearningPathOverviewOut)
def learning_path_overview(
    employee_id: str,
    db: Session = Depends(get_db),
    current: Employee = Depends(get_current_employee),
):
    """The full prerequisite graph for the employee's practice path, with
    a per-certification status against their own record.

    Self-only, same as /next (and for the same reason — see its
    docstring and the 2026-08-27 security-pass dev log entry: this had no
    identity check at all before that pass).

    /next above is the RULE-BASED ground truth of what's eligible right
    now — this endpoint is deliberately NOT that; it's the wider context
    (every node and edge in the path, not just the unlocked frontier) an
    LLM needs to reason about a real recommendation instead of just
    reciting a bare list: which locked track someone is closest to
    finishing, why a certification isn't available yet, which prerequisite
    is the one blocking something. Meant to be called alongside /next
    (never contradicts it — every fact here comes straight out of
    certification_prerequisites/certification_catalog, nothing inferred)
    and get_tracker_records for full submission history (dates,
    verification status, failed attempts) — see agent/adk_agent/prompts.py
    for how the agent is instructed to combine all three rather than
    guessing at eligibility itself.
    """
    if employee_id != current.employee_id:
        raise HTTPException(status_code=403, detail="Can only query your own learning path overview.")
    employee = current
    now = datetime.now(timezone.utc)

    practice = employee.practice
    if practice is None:
        return LearningPathOverviewOut(
            practice=None, path_name=None, certifications=[], data_as_of=now, practice_missing=True
        )

    path = _get_path(db, practice)
    if not path or not path.items:
        return LearningPathOverviewOut(practice=practice, path_name=path.name if path else None, certifications=[], data_as_of=now)

    path_cert_ids = {item.certification_id for item in path.items}

    records_by_cert_id: dict = {}
    for r in db.scalars(select(CertificateRecord).where(CertificateRecord.employee_id == employee.id)):
        if r.certification_id:
            # A cert can be resubmitted (e.g. failed, then retaken) — keep
            # the most recently created record's status per certification.
            existing = records_by_cert_id.get(r.certification_id)
            if not existing or r.created_at > existing.created_at:
                records_by_cert_id[r.certification_id] = r

    edges = db.scalars(
        select(CertificationPrerequisite).where(CertificationPrerequisite.certification_id.in_(path_cert_ids))
    ).all()
    edges_by_cert: dict = {}
    for edge in edges:
        edges_by_cert.setdefault(edge.certification_id, []).append(edge)

    statuses: list[PathCertificationStatusOut] = []
    for item in path.items:
        record = records_by_cert_id.get(item.certification_id)
        employee_status = record.submission_status if record else "not_started"
        incoming = edges_by_cert.get(item.certification_id, [])
        statuses.append(
            PathCertificationStatusOut(
                certification=item.certification,
                is_required=item.is_required,
                employee_status=employee_status,
                prerequisite_logic=item.certification.prerequisite_logic,
                incoming_prerequisites=[
                    PathPrerequisiteEdgeOut(prerequisite_name=e.prerequisite.name, track_label=e.track_label)
                    for e in incoming
                ],
            )
        )

    return LearningPathOverviewOut(practice=practice, path_name=path.name, certifications=statuses, data_as_of=now)


@router.post("/{path_id}/certifications", status_code=201)
def add_certification_to_path(
    path_id: str,
    payload: PathMembershipIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """add_certification_to_path: attach one certification to one
    already-existing path's membership. Idempotent — adding a
    certification that's already a member is a no-op, not an error."""
    path = db.get(LearningPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Learning path not found")
    cert = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == payload.certification_name))
    if not cert:
        raise HTTPException(status_code=422, detail=f"Unknown certification '{payload.certification_name}'.")

    existing = db.scalar(
        select(LearningPathItem).where(
            LearningPathItem.path_id == path.id, LearningPathItem.certification_id == cert.id
        )
    )
    if existing:
        return {"path_id": str(path.id), "certification": cert.name, "already_member": True}

    create_with_audit(
        db, LearningPathItem, actor=admin.employee_id,
        path_id=path.id, certification_id=cert.id, is_required=payload.is_required,
    )
    db.commit()
    return {"path_id": str(path.id), "certification": cert.name, "already_member": False}


@router.post("/certifications/{certification_name}/add-to-paths")
def add_certification_to_paths(
    certification_name: str,
    payload: BulkAddToPathsIn,
    db: Session = Depends(get_db),
    admin: Employee = Depends(require_admin),
):
    """add_certification_to_paths bulk variant: attach one certification to
    every path named in path_ids, or to ALL existing learning paths when
    all=True. Paths that already contain the certification are skipped,
    not duplicated."""
    cert = db.scalar(select(CertificationCatalog).where(CertificationCatalog.name == certification_name))
    if not cert:
        raise HTTPException(status_code=422, detail=f"Unknown certification '{certification_name}'.")

    if payload.all:
        paths = db.scalars(select(LearningPath)).all()
    elif payload.path_ids:
        paths = db.scalars(select(LearningPath).where(LearningPath.id.in_(payload.path_ids))).all()
    else:
        raise HTTPException(status_code=422, detail="Provide path_ids or set all=true.")

    already_member_path_ids = set(
        db.scalars(
            select(LearningPathItem.path_id).where(LearningPathItem.certification_id == cert.id)
        ).all()
    )

    added, skipped = [], []
    for path in paths:
        if path.id in already_member_path_ids:
            skipped.append(str(path.id))
            continue
        create_with_audit(
            db, LearningPathItem, actor=admin.employee_id,
            path_id=path.id, certification_id=cert.id, is_required=True,
        )
        added.append(str(path.id))

    db.commit()
    return {"certification": cert.name, "added_to_paths": added, "already_member_paths": skipped}


# Registered AFTER /next, deliberately: FastAPI/Starlette match routes in
# registration order, and this catch-all single-segment path param would
# otherwise swallow "/next" as if "next" were a practice name (exactly
# what happened when this was tested live — see 2026-08-24 dev log entry).
@router.get("/{practice}", response_model=LearningPathOut)
def get_learning_path(practice: str, db: Session = Depends(get_db)):
    path = _get_path(db, practice)
    if not path:
        raise HTTPException(status_code=404, detail=f"No learning path configured for practice '{practice}' yet")
    return path
